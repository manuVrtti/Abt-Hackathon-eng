"""Engine side of the live tester: scheduling, throttling, logging, scoring.

Rules (published to participants):
* every request carries the published User-Agent and an X-Eval-Request-ID;
* every request is stored with time, URL, request ID, status, body and latency;
* tests run 3 times spread over 6 hours, the first request of each attempt
  waits up to 90 seconds; the best attempt counts;
* a dead link scores zero for this component only; the team gets the log
  and one re-test on request.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import ENGINE_VERSION, db, jobs
from ..config import get_settings
from ..models import Appeal, Score, Submission, TestAttempt, TestRequest, TestResult
from ..tracks import get_track
from ..util import truncate, utcnow
from .framework import SuiteResult, TesterIdentity, run_suite

# Injected by tests to point the tester at in-process apps.
TRANSPORT: httpx.AsyncBaseTransport | None = None

_sems: dict[tuple[int, str], asyncio.Semaphore] = {}


def host_key(url: str) -> tuple[str, int]:
    cfg = get_settings().tester
    host = (urlparse(url).hostname or "").lower()
    for suffix in sorted(cfg.platform_suffixes, key=len, reverse=True):
        if host == suffix or host.endswith("." + suffix):
            return "platform:" + suffix, cfg.per_platform_concurrency
    return "host:" + host, cfg.per_host_concurrency


def _sem(url: str) -> asyncio.Semaphore:
    key, limit = host_key(url)
    loop_key = (id(asyncio.get_running_loop()), key)
    if loop_key not in _sems:
        _sems[loop_key] = asyncio.Semaphore(limit)
    return _sems[loop_key]


def identity() -> TesterIdentity:
    t = get_settings().tester
    return TesterIdentity(user_agent=t.user_agent, request_id_header=t.request_id_header,
                          warmup_timeout_s=t.warmup_timeout_s, request_timeout_s=t.request_timeout_s,
                          max_body_bytes=t.max_body_bytes)


def schedule_live_tests(s: Session, sub: Submission) -> None:
    t = get_settings().tester
    now = utcnow()
    for i in range(1, t.attempts + 1):
        offset = 0 if t.attempts == 1 else t.spread_hours * (i - 1) / (t.attempts - 1)
        jobs.enqueue(s, "livetest", f"{sub.id}:{i}", {"submission_id": sub.id, "attempt": i},
                     run_after=now + timedelta(hours=offset), max_attempts=2)


def schedule_retest(s: Session, sub_id: int) -> None:
    attempt = get_settings().tester.attempts + 1
    jobs.enqueue(s, "livetest", f"{sub_id}:{attempt}",
                 {"submission_id": sub_id, "attempt": attempt, "is_retest": True}, priority=10, max_attempts=2)


async def run_tests(sub_id: int, base_url: str, track_id: str) -> SuiteResult:
    track = get_track(track_id)
    tests = track.load_hidden()
    async with _sem(base_url):
        async with httpx.AsyncClient(follow_redirects=True, transport=TRANSPORT) as client:
            return await run_suite(tests, base_url, identity(), client=client, warmup_path=track.warmup_path)


def store_result(s: Session, sub_id: int, attempt: int, target: str, res: SuiteResult, is_retest: bool = False) -> TestAttempt:
    max_body = get_settings().tester.max_body_bytes
    # Safe re-run: replace this attempt's rows.
    s.execute(delete(TestRequest).where(TestRequest.submission_id == sub_id, TestRequest.attempt == attempt,
                                        TestRequest.target == target))
    s.execute(delete(TestResult).where(TestResult.submission_id == sub_id, TestResult.attempt == attempt,
                                       TestResult.target == target))
    for r in res.records:
        s.add(TestRequest(submission_id=sub_id, attempt=attempt, target=target, test_id=r.test_id,
                          request_id=r.request_id, method=r.method, url=r.url,
                          request_body=truncate(r.request_body, 8192), sent_at=r.sent_at,
                          status_code=r.status_code, latency_ms=r.latency_ms, response_headers=r.response_headers,
                          response_body=truncate(r.response_body, max_body), error=r.error))
    for o in res.outcomes:
        s.add(TestResult(submission_id=sub_id, attempt=attempt, target=target, test_id=o.test_id,
                         category=o.category, weight=o.weight, passed=o.passed, message=o.message,
                         duration_ms=o.duration_ms))
    ta = s.scalars(select(TestAttempt).where(TestAttempt.submission_id == sub_id, TestAttempt.attempt == attempt,
                                             TestAttempt.target == target)).first()
    if ta is None:
        ta = TestAttempt(submission_id=sub_id, attempt=attempt, target=target)
        s.add(ta)
    ta.is_retest = is_retest
    ta.started_at, ta.finished_at = res.started_at, res.finished_at
    ta.score, ta.passed, ta.total = res.score, res.passed, len(res.outcomes)
    ta.reachable, ta.error = res.reachable, res.error
    s.flush()
    return ta


def update_test_score(s: Session, sub_id: int) -> float:
    attempts = s.scalars(select(TestAttempt).where(TestAttempt.submission_id == sub_id,
                                                   TestAttempt.target == "live")).all()
    best = max((a for a in attempts if a.score is not None), key=lambda a: a.score, default=None)
    value = best.score if best else 0.0
    sc = s.scalars(select(Score).where(Score.submission_id == sub_id, Score.component == "tests")).first()
    if sc is None:
        sc = Score(submission_id=sub_id, component="tests")
        s.add(sc)
    sc.value, sc.engine_version = value, ENGINE_VERSION
    sc.detail = {"best_attempt": best.attempt if best else None,
                 "attempts": {a.attempt: a.score for a in attempts}, "rule": "best of all attempts"}
    return value


@jobs.handler("livetest")
async def livetest_job(p: dict) -> dict:
    cfg = get_settings().tester
    sub_id, attempt, is_retest = p["submission_id"], p["attempt"], p.get("is_retest", False)

    def load():
        with db.session() as s:
            sub = s.get(Submission, sub_id)
            best = s.scalar(select(func.max(TestAttempt.score)).where(
                TestAttempt.submission_id == sub_id, TestAttempt.target == "live"))
            return sub.live_url, sub.track, sub.status, best

    live_url, track, status, best = await asyncio.to_thread(load)
    if status != "active":
        return {"skipped": f"status={status}"}
    if not is_retest and cfg.skip_remaining_on_full_score and best is not None and best >= 100:
        return {"skipped": "already full score"}
    if not live_url:
        res = SuiteResult(base_url="", reachable=False, error="no live link submitted")
        tests = get_track(track).load_hidden()
        from .framework import TestOutcome

        res.outcomes = [TestOutcome(t.id, t.category, t.weight, False, "no live link submitted", 0) for t in tests]
        res.started_at = res.finished_at = utcnow()
    else:
        res = await run_tests(sub_id, live_url, track)

    def save():
        with db.session() as s:
            store_result(s, sub_id, attempt, "live", res, is_retest)
            value = update_test_score(s, sub_id)
            last_scheduled = attempt >= cfg.attempts
            if (last_scheduled or is_retest) and value == 0:
                _send_test_log(s, sub_id, offer_retest=not is_retest)
            return value

    value = await asyncio.to_thread(save)
    return {"attempt": attempt, "score": res.score, "best": value, "reachable": res.reachable}


def _send_test_log(s: Session, sub_id: int, offer_retest: bool) -> None:
    from ..notices import test_log_notice
    from ..review import issue_token

    sub = s.get(Submission, sub_id)
    retest = None
    if offer_retest:
        existing = s.scalars(select(Appeal).where(Appeal.submission_id == sub_id, Appeal.kind == "retest")).first()
        if existing is not None:
            return  # log already sent with the re-test link
        retest = issue_token(s, sub_id, 2, "retest", hours=get_settings().tester.retest_window_hours)
    test_log_notice(s, sub, retest)

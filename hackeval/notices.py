"""Evidence reports. Email is out of scope for now: each notice is written as
an HTML page (plus JSON) to data/outbox/<team_id>/ and listed in a CSV the
organisers can send or upload. Adding a mail backend later only needs a
function that reads undelivered Notice rows."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Appeal, Check, Flag, Notice, Submission, TestAttempt, TestRequest

_env = Environment(loader=PackageLoader("hackeval", "templates"), autoescape=select_autoescape())


def appeal_link(a: Appeal) -> str:
    base = get_settings().event.public_base_url.rstrip("/")
    return f"{base}/{'retest' if a.kind == 'retest' else 'appeal'}/{a.token}"


def _write(s: Session, sub: Submission, kind: str, subject: str, html: str, data: dict, links: dict) -> Notice:
    out = get_settings().path("outbox", sub.team_id)
    n = Notice(submission_id=sub.id, kind=kind, subject=subject, body_path="", links=links)
    s.add(n)
    s.flush()
    path = out / f"{n.id:06d}-{kind}.html"
    path.write_text(html, encoding="utf-8")
    (out / f"{n.id:06d}-{kind}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    n.body_path = str(path)
    return n


def evidence_notice(s: Session, sub: Submission, round_: int, reason: str, *, checks: list[Check],
                    flag: Flag | None, appeal: Appeal) -> Notice:
    cfg = get_settings()
    data = {
        "team_id": sub.team_id, "team_name": sub.team_name, "round": round_, "reason": reason,
        "frozen_commit": sub.frozen_sha, "frozen_at": sub.frozen_at,
        "checks": [{"rule": c.rule, "result": c.result, "summary": c.summary, "proof": c.proof,
                    "checked_at": c.created_at, "engine_version": c.engine_version} for c in checks],
        "flag": {"type": flag.type, "reason": flag.reason, "evidence": flag.evidence,
                 "reviewer_note": flag.decision_note} if flag else None,
        "appeal_url": appeal_link(appeal), "appeal_expires_at": appeal.expires_at,
    }
    html = _env.get_template("evidence.html").render(event=cfg.event.name, **data)
    subject = f"[{cfg.event.name}] Round {round_} result for {sub.team_name or sub.team_id}"
    return _write(s, sub, f"r{round_}-evidence", subject, html, data, {"appeal": data["appeal_url"]})


def test_log_notice(s: Session, sub: Submission, retest: Appeal | None) -> Notice:
    """Sent when a live link scores zero: the full request log plus a re-test link."""
    cfg = get_settings()
    attempts = s.scalars(select(TestAttempt).where(TestAttempt.submission_id == sub.id,
                                                   TestAttempt.target == "live").order_by(TestAttempt.attempt)).all()
    reqs = s.scalars(select(TestRequest).where(TestRequest.submission_id == sub.id,
                                               TestRequest.target == "live").order_by(TestRequest.id)).all()
    data = {
        "team_id": sub.team_id, "team_name": sub.team_name, "live_url": sub.live_url,
        "tester": {"ip": cfg.tester.published_ip, "user_agent": cfg.tester.user_agent,
                   "request_id_header": cfg.tester.request_id_header},
        "attempts": [{"attempt": a.attempt, "started_at": a.started_at, "score": a.score,
                      "reachable": a.reachable, "error": a.error} for a in attempts],
        "requests": [{"attempt": r.attempt, "request_id": r.request_id, "time": r.sent_at, "method": r.method,
                      "url": r.url, "status": r.status_code, "latency_ms": r.latency_ms,
                      "error": r.error, "body": (r.response_body or "")[:2000]} for r in reqs],
        "retest_url": appeal_link(retest) if retest else None,
        "retest_expires_at": retest.expires_at if retest else None,
    }
    html = _env.get_template("testlog.html").render(event=cfg.event.name, **data)
    subject = f"[{cfg.event.name}] Live link test log for {sub.team_name or sub.team_id}"
    return _write(s, sub, "r2-testlog", subject, html, data, {"retest": data["retest_url"]} if retest else {})


def export_outbox(s: Session, path: Path, only_new: bool = True, mark: bool = True) -> int:
    q = select(Notice, Submission).join(Submission, Submission.id == Notice.submission_id).order_by(Notice.id)
    if only_new:
        q = q.where(Notice.delivered.is_(False))
    rows = s.execute(q).all()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["notice_id", "team_id", "team_name", "email", "kind", "subject", "report_file",
                    "appeal_url", "retest_url", "created_at"])
        for n, sub in rows:
            w.writerow([n.id, sub.team_id, sub.team_name, sub.email, n.kind, n.subject, n.body_path,
                        n.links.get("appeal", ""), n.links.get("retest", ""), n.created_at.isoformat()])
            if mark:
                n.delivered = True
    return len(rows)

"""Proof that the live tester's verdicts are defensible."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hackeval import db, jobs
from hackeval.livetest import runner
from hackeval.livetest.framework import classify_error
from hackeval.models import Submission, TestAttempt, TestRequest
from hackeval.web import ping as ping_mod
from hackeval.web import public

from .todo_app import make_app


def _sub() -> int:
    with db.session() as s:
        sub = Submission(team_id="t", repo_url="x", live_url="https://t.test", track="sample_todo", round=2)
        s.add(sub)
        s.flush()
        return sub.id


def test_attempt_voided_when_tester_offline(settings, monkeypatch):
    """If our own internet is down, the attempt must not count against the team."""
    sid = _sub()
    settings.tester.canary_urls = ["https://canary.invalid/"]

    async def offline():
        return {"online": False, "checked": [{"url": "https://canary.invalid/", "error": "ConnectError"}]}

    monkeypatch.setattr(runner, "tester_online", offline)
    with pytest.raises(jobs.Retry, match="voided"):
        asyncio.run(runner.livetest_job({"submission_id": sid, "attempt": 1}))
    with db.session() as s:
        assert s.scalars(select(TestAttempt)).first() is None  # nothing recorded


def test_failure_records_kind_and_tester_check(settings, monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("Connection refused", request=request)

    monkeypatch.setattr(runner, "TRANSPORT", httpx.MockTransport(refuse))
    sid = _sub()
    res = asyncio.run(runner.livetest_job({"submission_id": sid, "attempt": 1}))
    assert res["reachable"] is False
    with db.session() as s:
        req = s.scalars(select(TestRequest)).first()
        att = s.scalars(select(TestAttempt)).one()
    assert req.error_kind == "connect" and req.request_id
    assert att.tester_check["before"]["online"] and att.tester_check["after"]["online"]
    assert "refused" in att.error


def test_error_classification():
    req = httpx.Request("GET", "https://x.test")
    assert classify_error(httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=req)) == "dns"
    assert classify_error(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]", request=req)) == "tls"
    assert classify_error(httpx.ConnectTimeout("t", request=req)) == "connect_timeout"
    assert classify_error(httpx.ReadTimeout("t", request=req)) == "read_timeout"


def test_ping_page_sends_tagged_request(settings, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(public, "PING_TRANSPORT", httpx.ASGITransport(app=make_app(seen_headers=seen)))
    monkeypatch.setattr(ping_mod, "_hits", ping_mod.defaultdict(ping_mod.deque))
    settings.event.deadline = settings.event.deadline.replace(year=2099)
    client = TestClient(public.app)
    assert "Tester IP" in client.get("/tester").text
    r = client.post("/tester", data={"url": "https://team.test/health"})
    assert r.status_code == 200 and "HTTP 200" in r.text
    rid = seen[0]["x-eval-request-id"]
    assert rid in r.text and seen[0]["user-agent"] == settings.tester.user_agent


def test_ping_refuses_private_addresses_and_after_deadline(settings, monkeypatch):
    monkeypatch.setattr(ping_mod, "_hits", ping_mod.defaultdict(ping_mod.deque))
    settings.event.deadline = settings.event.deadline.replace(year=2099)
    for bad in ("http://127.0.0.1:8000/", "http://localhost/", "http://169.254.169.254/latest/", "ftp://x.com"):
        with pytest.raises(ping_mod.PingRefused):
            asyncio.run(ping_mod.ping(bad, "v"))
    settings.event.deadline = settings.event.deadline.replace(year=2000)
    with pytest.raises(ping_mod.PingRefused, match="closed"):
        asyncio.run(ping_mod.ping("https://example.com", "v"))


def test_ping_rate_limit(settings, monkeypatch):
    monkeypatch.setattr(ping_mod, "_hits", ping_mod.defaultdict(ping_mod.deque))
    settings.event.deadline = settings.event.deadline.replace(year=2099)
    t = httpx.ASGITransport(app=make_app())
    for _ in range(ping_mod.PER_VISITOR[0]):
        asyncio.run(ping_mod.ping("https://team.test/health", "same-visitor", transport=t))
    with pytest.raises(ping_mod.PingRefused, match="Too many"):
        asyncio.run(ping_mod.ping("https://team.test/health", "same-visitor", transport=t))

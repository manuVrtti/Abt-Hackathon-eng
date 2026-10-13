"""Public pages participants reach from their evidence report.

    GET/POST /appeal/{token}   view the evidence and file an appeal
    GET/POST /retest/{token}   request the one live-link re-test (shows the full request log)
    GET/POST /tester           before the deadline: send one tagged request to your live link

Tokens are random and unguessable; each grants access to one team's record only.
"""

from __future__ import annotations

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, PackageLoader, select_autoescape
from sqlalchemy import select

from .. import db
from ..config import get_settings
from ..livetest.framework import ERROR_KINDS
from ..models import Appeal, Check, Flag, HARD_FAIL, Submission, TestAttempt, TestRequest
from ..review import file_appeal, redeem_retest
from ..util import aware, utcnow
from .ping import PingRefused, ping

# Tests inject an httpx transport here.
PING_TRANSPORT = None

app = FastAPI(title="Hackathon appeals", docs_url=None, redoc_url=None)
_env = Environment(loader=PackageLoader("hackeval", "templates"), autoescape=select_autoescape())


def _page(name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return HTMLResponse(_env.get_template(name).render(event=get_settings().event.name, **ctx),
                        status_code=status_code)


def _load(token: str, kind: str):
    with db.session() as s:
        a = s.scalars(select(Appeal).where(Appeal.token == token, Appeal.kind == kind)).first()
        if a is None:
            raise HTTPException(404, "Unknown link")
        sub = s.get(Submission, a.submission_id)
        if a.check_id:
            checks = [s.get(Check, a.check_id)]
        else:
            checks = s.scalars(select(Check).where(Check.submission_id == sub.id, Check.round == a.round,
                                                   Check.result == HARD_FAIL)).all()
        flag = s.get(Flag, a.flag_id) if a.flag_id else None
        s.expunge_all()
        return a, sub, checks, flag


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/appeal/{token}", response_class=HTMLResponse)
def appeal_get(token: str):
    a, sub, checks, flag = _load(token, "appeal")
    return _page("appeal.html", team_id=sub.team_id, team_name=sub.team_name, round=a.round,
                 reason=sub.status_reason, checks=checks, flag=flag, expires_at=a.expires_at,
                 status=a.status, message=a.message, note=a.decision_note,
                 closed=aware(a.expires_at) < utcnow())


@app.post("/appeal/{token}", response_class=HTMLResponse)
def appeal_post(token: str, message: str = Form(...)):
    try:
        file_appeal(token, message)
    except ValueError as e:
        return _page("message.html", 400, title="Appeal not accepted", message=str(e))
    return appeal_get(token)


@app.get("/retest/{token}", response_class=HTMLResponse)
def retest_get(token: str):
    a, sub, _, _ = _load(token, "retest")
    with db.session() as s:
        attempts = s.scalars(select(TestAttempt).where(TestAttempt.submission_id == sub.id,
                                                       TestAttempt.target == "live").order_by(TestAttempt.attempt)).all()
        reqs = s.scalars(select(TestRequest).where(TestRequest.submission_id == sub.id, TestRequest.target == "live")
                         .order_by(TestRequest.id)).all()
        s.expunge_all()
    t = get_settings().tester
    return _page("retest.html", team_id=sub.team_id, team_name=sub.team_name, live_url=sub.live_url,
                 expires_at=a.expires_at, used=a.status == "used", closed=aware(a.expires_at) < utcnow(),
                 warmup=int(t.warmup_timeout_s), attempts=attempts, requests=reqs, error_kinds=ERROR_KINDS,
                 tester={"ip": t.published_ip, "user_agent": t.user_agent, "header": t.request_id_header})


@app.post("/retest/{token}", response_class=HTMLResponse)
def retest_post(token: str):
    try:
        redeem_retest(token)
    except ValueError as e:
        return _page("message.html", 400, title="Re-test not scheduled", message=str(e))
    return retest_get(token)


def _tester_ctx() -> dict:
    cfg = get_settings()
    t = cfg.tester
    return {"tester": {"ip": t.published_ip, "user_agent": t.user_agent, "header": t.request_id_header,
                       "attempts": t.attempts, "spread_hours": t.spread_hours, "warmup": int(t.warmup_timeout_s)},
            "deadline": cfg.event.deadline, "open": t.ping_page_enabled and utcnow() <= cfg.event.deadline}


@app.get("/tester", response_class=HTMLResponse)
def tester_get():
    return _page("tester.html", result=None, error=None, url="", **_tester_ctx())


@app.post("/tester", response_class=HTMLResponse)
async def tester_post(request: Request, url: str = Form(...)):
    try:
        res = await ping(url, request.client.host if request.client else "unknown", transport=PING_TRANSPORT)
        return _page("tester.html", result=res, error=None, url=url, **_tester_ctx())
    except PingRefused as e:
        return _page("tester.html", 400, result=None, error=str(e), url=url, **_tester_ctx())

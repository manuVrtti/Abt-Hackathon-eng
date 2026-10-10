"""Public pages participants reach from their evidence report.

    GET/POST /appeal/{token}   view the evidence and file an appeal
    GET/POST /retest/{token}   request the one live-link re-test

Tokens are random and unguessable; each grants access to one team's record only.
"""

from __future__ import annotations

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse
from jinja2 import Environment, PackageLoader, select_autoescape
from sqlalchemy import select

from .. import db
from ..config import get_settings
from ..models import Appeal, Check, Flag, HARD_FAIL, Submission
from ..review import file_appeal, redeem_retest
from ..util import aware, utcnow

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
    return _page("retest.html", team_id=sub.team_id, team_name=sub.team_name, live_url=sub.live_url,
                 expires_at=a.expires_at, used=a.status == "used", closed=aware(a.expires_at) < utcnow(),
                 warmup=int(get_settings().tester.warmup_timeout_s))


@app.post("/retest/{token}", response_class=HTMLResponse)
def retest_post(token: str):
    try:
        redeem_retest(token)
    except ValueError as e:
        return _page("message.html", 400, title="Re-test not scheduled", message=str(e))
    return retest_get(token)

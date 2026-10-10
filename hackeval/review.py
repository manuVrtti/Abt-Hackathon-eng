"""Human-in-the-loop decisions: flags, disqualification, appeals, re-tests.

No flag ever disqualifies a team on its own; only a reviewer upholding it
does. Hard fails disqualify provisionally and always come with evidence and
an appeal link valid for the appeal window.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db
from .config import get_settings
from .models import (
    ACTIVE, DISQUALIFIED, HARD_FAIL, Appeal, Check, Flag, RoundState, Submission,
)
from .util import aware, new_token, utcnow


class RoundPaused(RuntimeError):
    pass


def raise_flag(s: Session, sub_id: int, round_: int, type_: str, reason: str,
               evidence: dict | None = None, related_id: int | None = None) -> Flag:
    """Create a flag unless an identical one (same team, type, related team) exists."""
    q = select(Flag).where(Flag.submission_id == sub_id, Flag.type == type_)
    q = q.where(Flag.related_submission_id == related_id) if related_id else q.where(
        Flag.related_submission_id.is_(None))
    f = s.scalars(q).first()
    if f is None:
        f = Flag(submission_id=sub_id, round=round_, type=type_, reason=reason,
                 evidence=evidence or {}, related_submission_id=related_id)
        s.add(f)
        s.flush()
    elif f.status == "open":
        f.reason, f.evidence = reason, evidence or f.evidence
    return f


def issue_token(s: Session, sub_id: int, round_: int, kind: str, *, check_id: int | None = None,
                flag_id: int | None = None, hours: int | None = None) -> Appeal:
    existing = s.scalars(select(Appeal).where(
        Appeal.submission_id == sub_id, Appeal.round == round_, Appeal.kind == kind,
        Appeal.check_id.is_(check_id) if check_id is None else Appeal.check_id == check_id,
        Appeal.flag_id.is_(flag_id) if flag_id is None else Appeal.flag_id == flag_id,
    )).first()
    if existing:
        return existing
    hours = hours if hours is not None else get_settings().appeal_window_hours
    a = Appeal(submission_id=sub_id, round=round_, kind=kind, token=new_token(),
               check_id=check_id, flag_id=flag_id, expires_at=utcnow() + timedelta(hours=hours))
    s.add(a)
    s.flush()
    return a


def disqualify(s: Session, sub: Submission, round_: int, reason: str, *, checks: list[Check] = (),
               flag: Flag | None = None, actor: str = "engine") -> Appeal:
    from .notices import evidence_notice

    sub.status = DISQUALIFIED
    sub.eliminated_round = round_
    sub.status_reason = reason
    appeal = issue_token(s, sub.id, round_, "appeal",
                         check_id=checks[0].id if len(checks) == 1 else None,
                         flag_id=flag.id if flag else None)
    evidence_notice(s, sub, round_, reason, checks=list(checks), flag=flag, appeal=appeal)
    db.log_event(s, actor, "disqualify", sub.id, round=round_, reason=reason)
    return appeal


def decide_flag(flag_id: int, decision: str, reviewer: str, note: str = "") -> None:
    """decision: upheld (disqualify), dismissed, or noted (keep team, record review)."""
    assert decision in ("upheld", "dismissed", "noted")
    with db.session() as s:
        f = s.get(Flag, flag_id)
        f.status, f.reviewer, f.decision_note, f.decided_at = decision, reviewer, note, utcnow()
        sub = s.get(Submission, f.submission_id)
        if decision == "upheld" and sub.status == ACTIVE:
            disqualify(s, sub, f.round, f"{f.type}: {f.reason}" + (f" Reviewer note: {note}" if note else ""),
                       flag=f, actor=reviewer)
        db.log_event(s, reviewer, f"flag_{decision}", f.submission_id, flag_id=flag_id, note=note)


def file_appeal(token: str, message: str) -> Appeal:
    with db.session() as s:
        a = s.scalars(select(Appeal).where(Appeal.token == token)).first()
        if a is None or a.kind != "appeal":
            raise ValueError("Unknown appeal link")
        if aware(a.expires_at) < utcnow():
            raise ValueError("This appeal window has closed")
        if a.status not in ("issued", "open"):
            raise ValueError("This appeal has already been decided")
        a.message, a.status, a.filed_at = message[:10000], "open", utcnow()
        db.log_event(s, "participant", "appeal_filed", a.submission_id, appeal_id=a.id)
        return a


def decide_appeal(appeal_id: int, accept: bool, reviewer: str, note: str = "") -> None:
    with db.session() as s:
        a = s.get(Appeal, appeal_id)
        a.status = "accepted" if accept else "rejected"
        a.reviewer, a.decision_note, a.decided_at = reviewer, note, utcnow()
        sub = s.get(Submission, a.submission_id)
        if accept:
            if a.check_id:
                s.get(Check, a.check_id).overturned = True
            else:
                for c in s.scalars(select(Check).where(Check.submission_id == sub.id, Check.result == HARD_FAIL)):
                    c.overturned = True
            if a.flag_id:
                s.get(Flag, a.flag_id).status = "dismissed"
            reinstate_if_clear(s, sub, actor=reviewer)
        db.log_event(s, reviewer, "appeal_" + a.status, sub.id, appeal_id=a.id, note=note)


def reinstate_if_clear(s: Session, sub: Submission, actor: str = "engine") -> bool:
    hard = s.scalars(select(Check).where(Check.submission_id == sub.id, Check.result == HARD_FAIL,
                                         Check.overturned.is_(False))).first()
    upheld = s.scalars(select(Flag).where(Flag.submission_id == sub.id, Flag.status == "upheld")).first()
    if hard or upheld or sub.status != DISQUALIFIED:
        return False
    sub.status, sub.status_reason, sub.eliminated_round = ACTIVE, "", None
    sub.round = max(sub.round, 2)
    db.log_event(s, actor, "reinstate", sub.id)
    from .r2 import enqueue_r2_for

    enqueue_r2_for(s, sub, only_if_started=True)
    return True


def redeem_retest(token: str) -> Appeal:
    """One live re-test per team, requested within the window after R2 tests."""
    from .livetest.runner import schedule_retest

    with db.session() as s:
        a = s.scalars(select(Appeal).where(Appeal.token == token)).first()
        if a is None or a.kind != "retest":
            raise ValueError("Unknown re-test link")
        if aware(a.expires_at) < utcnow():
            raise ValueError("The re-test window has closed")
        if a.status != "issued":
            raise ValueError("A re-test has already been requested for this team")
        a.status, a.filed_at = "used", utcnow()
        schedule_retest(s, a.submission_id)
        db.log_event(s, "participant", "retest_requested", a.submission_id)
        return a


def is_paused(s: Session, round_: int) -> RoundState | None:
    st = s.get(RoundState, round_)
    return st if st and st.paused else None


def ensure_round_open(round_: int) -> None:
    with db.session() as s:
        st = is_paused(s, round_)
        if st:
            raise RoundPaused(f"Round {round_} is paused: {st.reason}")


def set_paused(round_: int, paused: bool, reason: str, actor: str = "operator") -> None:
    with db.session() as s:
        st = s.get(RoundState, round_) or RoundState(round=round_)
        st.paused, st.reason = paused, reason
        s.merge(st)
        db.log_event(s, actor, "round_paused" if paused else "round_resumed", None, round=round_, reason=reason)

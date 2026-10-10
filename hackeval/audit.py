"""Random audit of automatic rejections (fairness commitment 5).

A person re-checks a random 2% of automatic rejections in each round. If
more than 1% of the audited decisions are wrong, the round is paused until
the cause is fixed: paused rounds refuse to advance.
"""

from __future__ import annotations

import math
import random

from sqlalchemy import select

from . import db
from .config import get_settings
from .models import DISQUALIFIED, ELIMINATED, HARD_FAIL, Audit, Check, EventLog, Submission
from .review import set_paused
from .util import utcnow


def automatic_rejections(s, round_: int) -> list[tuple[Submission, Check | None]]:
    out = []
    if round_ == 1:
        for sub in s.scalars(select(Submission).where(Submission.status == DISQUALIFIED,
                                                      Submission.eliminated_round == 1)):
            # Only engine decisions; reviewer-upheld flags were already human decisions.
            by_engine = s.scalars(select(EventLog).where(EventLog.submission_id == sub.id,
                                                         EventLog.action == "disqualify",
                                                         EventLog.actor == "engine")).first()
            if by_engine:
                c = s.scalars(select(Check).where(Check.submission_id == sub.id, Check.result == HARD_FAIL)).first()
                out.append((sub, c))
    else:
        for sub in s.scalars(select(Submission).where(Submission.status == ELIMINATED,
                                                      Submission.eliminated_round == round_)):
            out.append((sub, None))
    return out


def sample(round_: int, rate: float | None = None, seed: int | None = None) -> int:
    rate = rate if rate is not None else get_settings().audit.sample_rate
    with db.session() as s:
        pool = automatic_rejections(s, round_)
        already = {a.submission_id for a in s.scalars(select(Audit).where(Audit.round == round_))}
        pool = [p for p in pool if p[0].id not in already]
        if not pool:
            return 0
        k = min(len(pool), max(1, math.ceil(rate * (len(pool) + len(already))) - len(already)))
        if k <= 0:
            return 0
        rng = random.Random(seed)
        for sub, c in rng.sample(pool, k):
            s.add(Audit(round=round_, submission_id=sub.id, check_id=c.id if c else None,
                        reason=sub.status_reason or ""))
        db.log_event(s, "engine", "audit_sampled", None, round=round_, count=k, seed=seed)
        return k


def record(audit_id: int, correct: bool, reviewer: str, note: str = "") -> dict:
    with db.session() as s:
        a = s.get(Audit, audit_id)
        a.verdict = "correct" if correct else "wrong"
        a.reviewer, a.note, a.decided_at = reviewer, note, utcnow()
        round_ = a.round
    return evaluate(round_)


def evaluate(round_: int) -> dict:
    max_err = get_settings().audit.max_error_rate
    with db.session() as s:
        rows = s.scalars(select(Audit).where(Audit.round == round_)).all()
        total_rejections = len(automatic_rejections(s, round_))
    decided = [a for a in rows if a.verdict != "pending"]
    wrong = sum(1 for a in decided if a.verdict == "wrong")
    # Error rate is measured against the whole population of rejections the
    # sample represents: wrong/decided estimates the rejection error rate.
    rate = wrong / len(decided) if decided else 0.0
    paused = bool(decided) and rate > max_err
    if paused:
        set_paused(round_, True, f"Audit found {wrong} wrong of {len(decided)} audited rejections "
                                 f"({rate:.1%} > {max_err:.0%}).", actor="audit")
    return {"round": round_, "sampled": len(rows), "decided": len(decided), "wrong": wrong,
            "error_rate": round(rate, 4), "rejections": total_rejections, "paused": paused}

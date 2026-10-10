"""R2 orchestration and the development-authenticity component."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db, jobs
from .config import get_settings
from .livetest.runner import schedule_live_tests
from .models import ACTIVE, EventLog, Snapshot, Submission
from .plagiarism import max_similarity
from .quality import set_score


def r2_started(s: Session) -> bool:
    return s.scalars(select(EventLog).where(EventLog.action == "r2_started")).first() is not None


def enqueue_r2_for(s: Session, sub: Submission, only_if_started: bool = False) -> None:
    if only_if_started and not r2_started(s):
        return
    jobs.enqueue(s, "fetch_full", str(sub.id), {"submission_id": sub.id}, max_attempts=4)
    schedule_live_tests(s, sub)


def start_r2(actor: str = "operator") -> int:
    from .review import ensure_round_open

    ensure_round_open(1)
    n = 0
    with db.session() as s:
        if not r2_started(s):
            db.log_event(s, actor, "r2_started")
        for sub in s.scalars(select(Submission).where(Submission.status == ACTIVE, Submission.round >= 2)):
            enqueue_r2_for(s, sub)
            n += 1
    return n


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def authenticity_value(history: dict | None, max_sim: float, min_commits: int) -> tuple[float, dict]:
    """Commit pattern (50%) + absence of cross-team similarity (50%), 0-100."""
    plag = 100 * (1 - _clamp((max_sim - 0.3) / 0.5))
    if not history:
        pattern, parts = 0.0, {}
    else:
        parts = {
            "commit_count": 25 * _clamp(history["commit_count"] / (3 * max(min_commits, 1))),
            "work_sessions": 25 * _clamp(history["sessions"] / 6),
            "active_hours": 15 * _clamp(history["active_hours"] / 12),
            "first_commit_small": 20 * (1 - _clamp(history["first_commit_code_share"])),
            "no_giant_commit": 15 * (1 - _clamp((history["largest_commit_share"] - 0.3) / 0.7)),
        }
        pattern = sum(parts.values())
    value = round(0.5 * pattern + 0.5 * plag, 2)
    return value, {"commit_pattern": round(pattern, 2), "pattern_parts": {k: round(v, 2) for k, v in parts.items()},
                   "max_similarity": round(max_sim, 3), "originality_vs_teams": round(plag, 2)}


def compute_authenticity() -> int:
    cfg = get_settings()
    sims = max_similarity()
    n = 0
    with db.session() as s:
        for sub in s.scalars(select(Submission).where(Submission.status == ACTIVE, Submission.round >= 2)):
            snap = s.scalars(select(Snapshot).where(Snapshot.submission_id == sub.id,
                                                    Snapshot.kind == "freeze")).first()
            hist = (snap.detail or {}).get("history") if snap else None
            value, detail = authenticity_value(hist, sims.get(sub.id, 0.0), cfg.rules.min_commits)
            set_score(s, sub.id, "authenticity", value, detail)
            n += 1
    return n

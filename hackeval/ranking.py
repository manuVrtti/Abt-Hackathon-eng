"""Weighted R2 ranking, the cut-off review band, and the voting hand-off."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from . import ENGINE_VERSION, db
from .config import get_settings
from .models import ACTIVE, ELIMINATED, Flag, Score, Submission
from .quality import set_score
from .review import ensure_round_open, raise_flag

COMPONENTS = ("tests", "quality", "authenticity", "llm")


@dataclass
class Row:
    sub_id: int
    team_id: str
    team_name: str
    track: str
    repo_url: str
    live_url: str
    submitted_at: object
    comps: dict[str, float | None] = field(default_factory=dict)
    pre: float = 0.0
    total: float = 0.0
    rank: int = 0
    open_flags: list[str] = field(default_factory=list)
    llm_fallback: bool = False


def _rows(min_round: int = 2) -> list[Row]:
    with db.session() as s:
        subs = s.scalars(select(Submission).where(Submission.status == ACTIVE, Submission.round >= min_round)).all()
        scores: dict[int, dict[str, float]] = {}
        for sc in s.scalars(select(Score).where(Score.component.in_(COMPONENTS))):
            scores.setdefault(sc.submission_id, {})[sc.component] = sc.value
        flags: dict[int, list[str]] = {}
        for f in s.scalars(select(Flag).where(Flag.status == "open")):
            flags.setdefault(f.submission_id, []).append(f.type)
        return [Row(x.id, x.team_id, x.team_name, x.track, x.repo_url, x.live_url, x.submitted_at,
                    {c: scores.get(x.id, {}).get(c) for c in COMPONENTS}, open_flags=flags.get(x.id, []))
                for x in subs]


def _sort_key(r: Row, attr: str):
    ts = r.submitted_at.timestamp() if r.submitted_at else float("inf")
    return (-getattr(r, attr), -(r.comps.get("tests") or 0), ts, r.team_id)


def preliminary() -> list[Row]:
    """Rank by tests, quality and authenticity only (weights renormalised).
    The top llm.top_n of this ranking get the LLM rubric."""
    w = get_settings().weights
    base = w.tests + w.quality + w.authenticity
    rows = _rows()
    for r in rows:
        c = r.comps
        r.pre = round((w.tests * (c["tests"] or 0) + w.quality * (c["quality"] or 0)
                       + w.authenticity * (c["authenticity"] or 0)) / base, 4)
    rows.sort(key=lambda r: _sort_key(r, "pre"))
    for i, r in enumerate(rows, 1):
        r.rank = i
    return rows


def final_ranking(write: bool = True) -> list[Row]:
    cfg = get_settings()
    w = cfg.weights
    rows = preliminary()
    top_n = {r.sub_id for r in rows[: cfg.llm.top_n]}
    for r in rows:
        c = r.comps
        llm = c["llm"]
        if llm is None and r.sub_id in top_n:
            # LLM failed for a team that should have had it: use the renormalised
            # score so the team is not penalised, and flag it for a human.
            llm, r.llm_fallback = r.pre, True
        r.total = round(w.tests * (c["tests"] or 0) + w.quality * (c["quality"] or 0)
                        + w.authenticity * (c["authenticity"] or 0) + w.llm * (llm or 0), 4)
    # Only teams that had the LLM pass can compete for the top places.
    rows.sort(key=lambda r: (r.sub_id not in top_n, *_sort_key(r, "total")))
    for i, r in enumerate(rows, 1):
        r.rank = i
    if write:
        with db.session() as s:
            for r in rows:
                set_score(s, r.sub_id, "total", r.total, {
                    "rank": r.rank, "components": r.comps, "pre_score": r.pre, "llm_fallback": r.llm_fallback,
                    "weights": w.model_dump(), "engine_version": ENGINE_VERSION})
                if r.llm_fallback:
                    raise_flag(s, r.sub_id, 2, "llm_missing", "LLM rubric failed; renormalised score used.", {})
    return rows


def select_for_voting(rows: list[Row]) -> list[Row]:
    rk = get_settings().ranking
    if not rk.track_quotas:
        return rows[: rk.to_voting]
    picked: list[Row] = []
    for track, quota in rk.track_quotas.items():
        picked += [r for r in rows if r.track == track][:quota]
    return sorted(picked, key=lambda r: r.rank)


def flag_cutoff_band(rows: list[Row]) -> int:
    """A person reviews the teams around the cut-off before results are published."""
    rk = get_settings().ranking
    lo, hi = max(1, rk.to_voting - rk.cutoff_review_band + 1), rk.to_voting + rk.cutoff_review_band
    n = 0
    with db.session() as s:
        for r in rows:
            if lo <= r.rank <= hi:
                raise_flag(s, r.sub_id, 2, "cutoff_review",
                           f"Rank {r.rank} is within {rk.cutoff_review_band} places of the voting cut-off "
                           f"({rk.to_voting}). Check the score breakdown before publishing.",
                           {"rank": r.rank, "total": r.total, "components": r.comps, "open_flags": r.open_flags})
                n += 1
    return n


def export_voting(path: Path, finalize: bool = False) -> int:
    """Write the top teams as CSV for the website. With finalize=True, the
    exported teams move to round 3 and the rest are marked eliminated in R2."""
    if finalize:
        ensure_round_open(2)
    rows = final_ranking(write=True)
    chosen = select_for_voting(rows)
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["rank", "team_id", "team_name", "track", "repo_url", "live_url", "total",
                     "tests", "quality", "authenticity", "llm"])
        for r in chosen:
            wr.writerow([r.rank, r.team_id, r.team_name, r.track, r.repo_url, r.live_url, r.total,
                         *[r.comps.get(c) for c in COMPONENTS]])
    if finalize:
        ids = {r.sub_id for r in chosen}
        with db.session() as s:
            for sub in s.scalars(select(Submission).where(Submission.status == ACTIVE, Submission.round >= 2)):
                if sub.id in ids:
                    sub.round = max(sub.round, 3)
                elif sub.round == 2:
                    sub.status, sub.eliminated_round = ELIMINATED, 2
                    sub.status_reason = "Below the voting cut-off in the R2 ranking."
            db.log_event(s, "operator", "voting_exported", None, count=len(ids))
    return len(chosen)


def import_voted(path: Path) -> dict:
    """Import the teams that came back from website voting (team_id column,
    optional votes column). They move to round 4; other R3 teams are eliminated."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    ids = {(r.get("team_id") or "").strip() for r in rows}
    votes = {(r.get("team_id") or "").strip(): r.get("votes") for r in rows}
    out = {"advanced": 0, "eliminated": 0, "unknown": 0}
    with db.session() as s:
        known = {x.team_id for x in s.scalars(select(Submission))}
        out["unknown"] = len(ids - known - {""})
        for sub in s.scalars(select(Submission).where(Submission.status == ACTIVE, Submission.round == 3)):
            if sub.team_id in ids:
                sub.round = 4
                if votes.get(sub.team_id):
                    try:
                        set_score(s, sub.id, "votes", float(votes[sub.team_id]), {})
                    except ValueError:
                        pass
                out["advanced"] += 1
            else:
                sub.status, sub.eliminated_round = ELIMINATED, 3
                sub.status_reason = "Not selected in public voting."
                out["eliminated"] += 1
        db.log_event(s, "operator", "voted_imported", None, **out)
    return out

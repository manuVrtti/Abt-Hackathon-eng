"""Dry run on a past hackathon: compare the engine's decisions with what happened.

Run the normal pipeline with ``event.replay: true`` and the past event's
start/deadline, then call ``compare`` with a CSV of past outcomes:

    team_id, past_status, past_rank
    T001,    disqualified,
    T002,    finalist,      4

past_status values: disqualified, eliminated, shortlisted, finalist, winner.
Either column may be empty.
"""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

from sqlalchemy import select

from . import db
from .config import get_settings
from .llm.calibrate import _pearson, _ranks
from .models import DISQUALIFIED, Check, HARD_FAIL, Score, Submission


def compare(outcomes_csv: Path) -> dict:
    with open(outcomes_csv, newline="", encoding="utf-8-sig") as f:
        past = {r["team_id"].strip(): r for r in csv.DictReader(f) if r.get("team_id")}
    with db.session() as s:
        subs = {x.team_id: x for x in s.scalars(select(Submission))}
        totals = {sc.submission_id: sc for sc in s.scalars(select(Score).where(Score.component == "total"))}
        fails: dict[int, list[str]] = {}
        for c in s.scalars(select(Check).where(Check.result == HARD_FAIL, Check.overturned.is_(False))):
            fails.setdefault(c.submission_id, []).append(c.rule)

    matrix = {"both_rejected": [], "only_engine_rejected": [], "only_past_rejected": [], "both_kept": 0}
    past_rank, our_rank = [], []
    missed_finalists = []
    cut = get_settings().ranking.to_voting
    for team, row in past.items():
        sub = subs.get(team)
        if sub is None:
            continue
        past_dq = (row.get("past_status") or "").strip().lower() == "disqualified"
        ours_dq = sub.status == DISQUALIFIED and sub.eliminated_round == 1
        if past_dq and ours_dq:
            matrix["both_rejected"].append(team)
        elif ours_dq:
            matrix["only_engine_rejected"].append({"team_id": team, "rules": fails.get(sub.id, []),
                                                   "reason": sub.status_reason})
        elif past_dq:
            matrix["only_past_rejected"].append(team)
        else:
            matrix["both_kept"] += 1
        t = totals.get(sub.id)
        rank = (t.detail or {}).get("rank") if t else None
        if (row.get("past_rank") or "").strip() and rank:
            past_rank.append(float(row["past_rank"]))
            our_rank.append(float(rank))
        if (row.get("past_status") or "").strip().lower() in ("finalist", "winner") and (not rank or rank > cut):
            missed_finalists.append({"team_id": team, "our_rank": rank, "status": sub.status,
                                     "reason": sub.status_reason})
    report = {
        "teams_compared": sum(1 for t in past if t in subs),
        "r1": {k: (v if isinstance(v, int) else len(v)) for k, v in matrix.items()},
        "r1_detail": {"only_engine_rejected": matrix["only_engine_rejected"],
                      "only_past_rejected": matrix["only_past_rejected"]},
        "rank_spearman": _pearson(_ranks(past_rank), _ranks(our_rank)) if past_rank else None,
        "rank_pairs": len(past_rank),
        "median_abs_rank_shift": statistics.median(abs(a - b) for a, b in zip(past_rank, our_rank))
        if past_rank else None,
        "past_finalists_below_our_cutoff": missed_finalists,
    }
    out = get_settings().path("reports") / "replay_comparison.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    report["report_file"] = str(out)
    return report

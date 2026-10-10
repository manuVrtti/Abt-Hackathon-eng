"""R5 judging and R6 finale.

Judges declare conflicts; assignments skip conflicted judges. Each judge's
weighted totals are converted to z-scores across the teams that judge
scored, so a strict judge cannot sink a team and a generous one cannot lift
it. Teams are ranked by their mean z-score.
"""

from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select

from . import db
from .config import get_settings
from .models import (
    ACTIVE, ELIMINATED, Judge, JudgeAssignment, JudgeConflict, JudgeScore, Submission,
)
from .quality import set_score
from .util import new_token


def criteria_for(round_: int) -> dict[str, float]:
    j = get_settings().judging
    return j.criteria if round_ == 5 else j.finale_criteria


def import_judges(path: Path) -> int:
    """CSV: name, email, conflicts (team_ids separated by ';')."""
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f, db.session() as s:
        teams = {x.team_id: x.id for x in s.scalars(select(Submission))}
        for row in csv.DictReader(f):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            j = s.scalars(select(Judge).where(Judge.name == name)).first()
            if j is None:
                j = Judge(name=name, passcode=new_token()[:10])
                s.add(j)
                s.flush()
            j.email = (row.get("email") or "").strip()
            for t in filter(None, (x.strip() for x in (row.get("conflicts") or "").split(";"))):
                if t in teams and not s.scalars(select(JudgeConflict).where(
                        JudgeConflict.judge_id == j.id, JudgeConflict.submission_id == teams[t])).first():
                    s.add(JudgeConflict(judge_id=j.id, submission_id=teams[t], note="declared"))
            n += 1
    return n


def assign(round_: int, per_team: int | None = None) -> dict:
    """Balanced assignment of judges to the round's teams, skipping conflicts."""
    per_team = per_team or get_settings().judging.judges_per_team
    with db.session() as s:
        judges = s.scalars(select(Judge).order_by(Judge.id)).all()
        subs = s.scalars(select(Submission).where(Submission.status == ACTIVE, Submission.round >= round_)
                         .order_by(Submission.id)).all()
        conflicts = {(c.judge_id, c.submission_id) for c in s.scalars(select(JudgeConflict))}
        load = defaultdict(int)
        for a in s.scalars(select(JudgeAssignment).where(JudgeAssignment.round == round_)):
            load[a.judge_id] += 1
        short = []
        for sub in subs:
            have = {a.judge_id for a in s.scalars(select(JudgeAssignment).where(
                JudgeAssignment.round == round_, JudgeAssignment.submission_id == sub.id))}
            pool = sorted((j for j in judges if j.id not in have and (j.id, sub.id) not in conflicts),
                          key=lambda j: (load[j.id], j.id))
            for j in pool[: max(0, per_team - len(have))]:
                s.add(JudgeAssignment(judge_id=j.id, submission_id=sub.id, round=round_))
                load[j.id] += 1
                have.add(j.id)
            if len(have) < per_team:
                short.append(sub.team_id)
    return {"teams": len(subs), "judges": len(judges), "understaffed": short}


def record_score(judge_id: int, sub_id: int, round_: int, values: dict[str, float], comment: str = "") -> None:
    crit = criteria_for(round_)
    mx = get_settings().judging.scale_max
    with db.session() as s:
        if s.scalars(select(JudgeConflict).where(JudgeConflict.judge_id == judge_id,
                                                 JudgeConflict.submission_id == sub_id)).first():
            raise ValueError("This judge declared a conflict of interest with this team.")
        for k, v in values.items():
            if k not in crit:
                raise ValueError(f"unknown criterion {k}")
            if not 0 <= v <= mx:
                raise ValueError(f"{k} must be between 0 and {mx}")
            js = s.scalars(select(JudgeScore).where(JudgeScore.judge_id == judge_id, JudgeScore.submission_id == sub_id,
                                                    JudgeScore.round == round_, JudgeScore.criterion == k)).first()
            if js is None:
                js = JudgeScore(judge_id=judge_id, submission_id=sub_id, round=round_, criterion=k)
                s.add(js)
            js.value, js.comment = float(v), comment
        db.log_event(s, f"judge:{judge_id}", "judge_score", sub_id, round=round_)


def results(round_: int, write: bool = True) -> list[dict]:
    crit = criteria_for(round_)
    wsum = sum(crit.values())
    with db.session() as s:
        rows = s.scalars(select(JudgeScore).where(JudgeScore.round == round_)).all()
        subs = {x.id: x for x in s.scalars(select(Submission).where(Submission.round >= round_))}
        judges = {j.id: j.name for j in s.scalars(select(Judge))}
    per: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)
    for r in rows:
        per[(r.judge_id, r.submission_id)][r.criterion] = r.value
    totals: dict[tuple[int, int], float] = {}
    for key, vals in per.items():
        if set(vals) >= set(crit):  # only complete score sheets count
            totals[key] = sum(crit[c] * vals[c] for c in crit) / wsum
    by_judge: dict[int, list[float]] = defaultdict(list)
    for (j, _), t in totals.items():
        by_judge[j].append(t)
    stats = {j: (statistics.fmean(v), statistics.pstdev(v) if len(v) > 1 else 0.0) for j, v in by_judge.items()}
    team: dict[int, dict] = defaultdict(lambda: {"z": [], "raw": [], "judges": []})
    for (j, sid), t in totals.items():
        mean, sd = stats[j]
        z = (t - mean) / sd if sd > 0 else 0.0
        team[sid]["z"].append(z)
        team[sid]["raw"].append(t)
        team[sid]["judges"].append(judges.get(j, str(j)))
    out = []
    for sid, d in team.items():
        if sid not in subs:
            continue
        out.append({"submission_id": sid, "team_id": subs[sid].team_id, "team_name": subs[sid].team_name,
                    "normalised": round(statistics.fmean(d["z"]), 4), "raw_mean": round(statistics.fmean(d["raw"]), 3),
                    "judges": d["judges"], "n_judges": len(d["z"])})
    out.sort(key=lambda r: (-r["normalised"], -r["raw_mean"], r["team_id"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    if write:
        with db.session() as s:
            for r in out:
                set_score(s, r["submission_id"], f"judging_r{round_}", r["normalised"], r)
    return out


def advance(round_: int, keep: int) -> dict:
    """Move the top `keep` teams of round 5 to the finale (round 6), or record
    the finale placing for round 6."""
    res = results(round_)
    keep_ids = {r["submission_id"] for r in res[:keep]}
    with db.session() as s:
        for sub in s.scalars(select(Submission).where(Submission.status == ACTIVE, Submission.round == round_)):
            if sub.id in keep_ids:
                if round_ == 5:
                    sub.round = 6
            elif round_ == 5:
                sub.status, sub.eliminated_round = ELIMINATED, 5
                sub.status_reason = "Not selected for the finale by the judges."
        db.log_event(s, "operator", f"r{round_}_advanced", None, kept=len(keep_ids))
    return {"ranked": len(res), "kept": len(keep_ids), "top": res[:keep]}

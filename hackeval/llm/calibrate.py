"""Test the rubric against hand-scored entries before the event.

Input CSV: team_id, scorer (optional), then one column per rubric criterion
(1-5). The listed teams must be imported and have a full code snapshot (for
a dry run, import the previous hackathon's submissions first).
"""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

from sqlalchemy import select

from .. import db
from ..config import get_settings
from ..models import HandScore, LLMScore, Submission
from .rubric import load_rubric
from .scoring import score_submissions

CALIBRATION_PASS = 90  # kept apart from real passes 1 and 2


def import_hand_scores(path: Path) -> int:
    rubric = load_rubric(get_settings().llm.rubric_path)
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f, db.session() as s:
        by_team = {x.team_id: x.id for x in s.scalars(select(Submission))}
        for row in csv.DictReader(f):
            sid = by_team.get((row.get("team_id") or "").strip())
            if sid is None:
                continue
            crit = {k: int(row[k]) for k in rubric.keys if row.get(k, "").strip()}
            scorer = (row.get("scorer") or "human").strip()
            hs = s.scalars(select(HandScore).where(HandScore.submission_id == sid, HandScore.scorer == scorer)).first()
            if hs is None:
                hs = HandScore(submission_id=sid, scorer=scorer)
                s.add(hs)
            hs.criteria = crit
            n += 1
    return n


def _pearson(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3 or statistics.pstdev(a) == 0 or statistics.pstdev(b) == 0:
        return None
    return round(statistics.correlation(a, b), 3)


def _ranks(v: list[float]) -> list[float]:
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def calibrate(run_llm: bool = True) -> dict:
    rubric = load_rubric(get_settings().llm.rubric_path)
    with db.session() as s:
        hand = s.scalars(select(HandScore)).all()
        ids = sorted({h.submission_id for h in hand})
    if run_llm:
        score_submissions(ids, CALIBRATION_PASS, batch=False)
    with db.session() as s:
        llm = {r.submission_id: r for r in s.scalars(select(LLMScore).where(LLMScore.pass_no == CALIBRATION_PASS))}
        hand = s.scalars(select(HandScore)).all()
    report: dict = {"entries": 0, "llm_errors": 0, "criteria": {}}
    per_crit: dict[str, tuple[list, list]] = {k: ([], []) for k in rubric.keys}
    for h in hand:
        r = llm.get(h.submission_id)
        if r is None or r.value is None:
            report["llm_errors"] += 1
            continue
        report["entries"] += 1
        for k in rubric.keys:
            if k in h.criteria and k in r.criteria:
                per_crit[k][0].append(float(h.criteria[k]))
                per_crit[k][1].append(float(r.criteria[k]["score"]))
    for k, (hv, lv) in per_crit.items():
        if not hv:
            continue
        diffs = [abs(a - b) for a, b in zip(hv, lv)]
        report["criteria"][k] = {
            "n": len(hv),
            "pearson": _pearson(hv, lv),
            "spearman": _pearson(_ranks(hv), _ranks(lv)),
            "mean_abs_error": round(sum(diffs) / len(diffs), 3),
            "exact_agreement": round(sum(d == 0 for d in diffs) / len(diffs), 3),
            "within_one": round(sum(d <= 1 for d in diffs) / len(diffs), 3),
            "mean_bias_llm_minus_human": round(sum(b - a for a, b in zip(hv, lv)) / len(hv), 3),
        }
    out = get_settings().path("reports") / "llm_calibration.json"
    out.write_text(json.dumps(report, indent=2))
    report["report_file"] = str(out)
    return report

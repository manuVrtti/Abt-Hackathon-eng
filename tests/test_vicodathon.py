"""Vicodathon setup: several problem statements ranked separately, vibe-coding rules."""

from __future__ import annotations

import yaml
from sqlalchemy import select

from hackeval import db
from hackeval.config import Settings
from hackeval.jobs import Job
from hackeval.llm.rubric import load_rubric
from hackeval.models import LLMScore, Score, Submission, TestAttempt
from hackeval.plagiarism import run_plagiarism
from hackeval.quality import assess
from hackeval.ranking import allocate, final_ranking, near_cutoff, select_for_voting
from hackeval.review import change_track

from .conftest import ROOT, code_file


def test_allocate_is_proportional():
    assert allocate(200, {"ps1": 3000, "ps2": 1500, "ps3": 500}) == {"ps1": 120, "ps2": 60, "ps3": 20}
    small = allocate(10, {"big": 990, "tiny": 10})
    assert small["tiny"] == 1 and sum(small.values()) == 10  # every track gets a place
    assert allocate(50, {"a": 3, "b": 4}) == {"a": 3, "b": 4}  # never more places than teams
    odd = allocate(7, {"a": 1, "b": 1, "c": 1})
    assert sum(odd.values()) == 3


def _team(s, tid: str, track: str, tests: float) -> int:
    sub = Submission(team_id=tid, repo_url="x", track=track, round=2)
    s.add(sub)
    s.flush()
    for comp, v in (("tests", tests), ("quality", 50), ("authenticity", 0), ("llm", 50)):
        s.add(Score(submission_id=sub.id, component=comp, value=v, engine_version="t"))
    return sub.id


def test_hard_track_is_not_sunk(settings):
    """ps2's tests are much harder, but its best teams still get places."""
    settings.ranking.per_track = True
    settings.ranking.to_voting = 4
    settings.weights.authenticity = 0.0
    with db.session() as s:
        for i in range(6):
            _team(s, f"easy{i}", "ps1", 90 - i)
        for i in range(6):
            _team(s, f"hard{i}", "ps2", 30 - i)
    rows = final_ranking()
    chosen = {r.team_id for r in select_for_voting(rows)}
    assert chosen == {"easy0", "easy1", "hard0", "hard1"}
    settings.ranking.per_track = False
    rows = final_ranking(write=False)
    assert {r.team_id for r in select_for_voting(rows)} == {"easy0", "easy1", "easy2", "easy3"}


def test_cutoff_band_is_per_track(settings):
    settings.ranking.per_track = True
    settings.ranking.to_voting = 4
    with db.session() as s:
        for i in range(6):
            _team(s, f"a{i}", "ps1", 90 - i)
            _team(s, f"b{i}", "ps2", 40 - i)
    near = {r.team_id for r in near_cutoff(final_ranking(write=False), 1)}
    assert near == {"a1", "a2", "b1", "b2"}


def test_change_track_clears_and_retests(settings):
    with db.session() as s:
        sid = _team(s, "t1", "sample_todo", 0)
        s.add(TestAttempt(submission_id=sid, attempt=1, target="live", score=0))
        s.add(LLMScore(submission_id=sid, pass_no=1, provider="mock", model="m", value=40))
    import shutil

    other = settings.tracks_dir / "sample_todo_copy"
    shutil.copytree(settings.tracks_dir / "sample_todo", other)
    try:
        res = change_track(sid, "sample_todo_copy", "reviewer1", "picked the wrong problem on the form")
    finally:
        shutil.rmtree(other)
    assert res["changed"] and res["cleared_attempts"] == 1 and res["retest_queued"]
    with db.session() as s:
        assert s.get(Submission, sid).track == "sample_todo_copy"
        assert not s.scalars(select(TestAttempt)).all() and not s.scalars(select(LLMScore)).all()
        assert not s.scalars(select(Score).where(Score.component == "tests")).all()
        assert len(s.scalars(select(Job).where(Job.kind == "livetest")).all()) == settings.tester.attempts


def test_vibe_settings(settings, tmp_path):
    cfg = Settings.model_validate(yaml.safe_load((ROOT / "vicodathon.example.yaml").read_text()))
    assert cfg.ranking.per_track and not cfg.plagiarism.enabled and not cfg.rules.first_commit_check
    assert abs(cfg.weights.tests + cfg.weights.llm + cfg.weights.quality + cfg.weights.authenticity - 1) < 1e-9
    rub = load_rubric(cfg.llm.rubric_path)
    assert rub.keys == ["problem_fit", "completeness", "product_thinking"]

    settings.plagiarism.enabled = False
    assert run_plagiarism()["disabled"] is True

    settings.quality.parts = cfg.quality.parts
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "app.py").write_text(code_file(1))
    (repo / "README.md").write_text("# App\n## Setup\nRun it.\n")
    _value, detail = assess(repo)
    assert "static" not in detail["parts"] and set(detail["parts"]) == {"readme", "secrets", "hygiene"}

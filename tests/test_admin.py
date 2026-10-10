from __future__ import annotations

import pytest

from hackeval import db
from hackeval.models import Check, Flag, Judge, JudgeAssignment, Submission
from hackeval.review import disqualify

from .conftest import ROOT

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP = str(ROOT / "hackeval" / "admin" / "app.py")


def seed():
    with db.session() as s:
        a = Submission(team_id="t1", team_name="One", repo_url="https://github.com/a/b", round=2)
        b = Submission(team_id="t2", team_name="Two", repo_url="https://github.com/c/d", round=5)
        s.add_all([a, b])
        s.flush()
        s.add(Flag(submission_id=a.id, round=1, type="first_commit_dump", reason="90% in first commit",
                   evidence={"share": 0.9}))
        c = Check(submission_id=a.id, round=1, rule="min_commits", result="hard_fail", engine_version="t",
                  summary="2 commits", proof={})
        s.add(c)
        s.flush()
        disqualify(s, a, 1, "2 commits", checks=[c])
        j = Judge(name="Asha", passcode="pc123")
        s.add(j)
        s.flush()
        s.add(JudgeAssignment(judge_id=j.id, submission_id=b.id, round=5))


def test_reviewer_pages_render(settings, monkeypatch):
    monkeypatch.setenv("HACKEVAL_ADMIN_PASSWORD", "secret")
    seed()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.text_input[0].input("Rev")
    at.text_input[1].input("secret")
    at.button[0].click().run()
    assert not at.exception
    for page in ["Dashboard", "Flag queue", "Appeals", "Team lookup", "R2 ranking", "Audit", "Sandbox (R4)",
                 "Judging results"]:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, (page, at.exception)


def test_judge_form(settings):
    seed()
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.radio[0].set_value("Judge").run()
    at.text_input[0].input("Asha")
    at.text_input[1].input("pc123")
    at.button[0].click().run()
    assert not at.exception
    assert any("Scoring: Asha" in h.value for h in at.header)
    save = next(b for b in at.button if b.label == "Save scores")
    save.click().run()
    assert not at.exception
    from sqlalchemy import select

    from hackeval.models import JudgeScore

    with db.session() as s:
        assert len(s.scalars(select(JudgeScore)).all()) == len(settings.judging.criteria)

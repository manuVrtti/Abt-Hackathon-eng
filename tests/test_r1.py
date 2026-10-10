from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import select

from hackeval import db, jobs
from hackeval.models import Appeal, Check, Flag, Notice, Snapshot, Submission
from hackeval.review import decide_appeal, decide_flag, file_appeal
from hackeval.rules import apply_r1_decisions, cross_team_checks
from hackeval.snapshot import freeze_heads

from .conftest import START, code_file, git, make_repo
from datetime import timedelta


def add(team: str, repo: Path | str, live: str = "") -> int:
    with db.session() as s:
        sub = Submission(team_id=team, team_name=team.title(), repo_url=str(repo), live_url=live,
                         track="sample_todo")
        s.add(sub)
        s.flush()
        return sub.id


def setup_repos(tmp: Path) -> dict[str, Path]:
    good = make_repo(tmp / "good", [{"app.py": code_file(i), "README.md": "# Good\n"} if i == 0 else
                                    {f"mod{i}.py": code_file(i)} for i in range(6)])
    few = make_repo(tmp / "few", [{"a.py": code_file(1)}, {"b.py": code_file(2)}])
    dump = make_repo(tmp / "dump", [{f"m{i}.py": code_file(100 + i) for i in range(8)}] +
                     [{"notes.md": f"note {i}"} for i in range(5)])
    copy = tmp / "copy"
    shutil.copytree(good, copy)
    old = make_repo(tmp / "old", [{f"o{i}.py": code_file(200 + i)} for i in range(6)],
                    start=START - timedelta(days=30))
    return {"good": good, "few": few, "dump": dump, "copy": copy, "old": old}


def test_r1_end_to_end(settings, tmp_path):
    repos = setup_repos(tmp_path / "repos")
    ids = {name: add(name, path) for name, path in repos.items()}
    ids["missing"] = add("missing", f"file://{tmp_path}/does-not-exist")

    stats = freeze_heads()
    assert stats["ls_remote"] == 5 and stats["unresolved"] == 1
    jobs.drain()

    with db.session() as s:
        snaps = {sn.submission_id: sn for sn in s.scalars(select(Snapshot))}
        assert all(snaps[ids[n]].ok for n in repos)
        assert snaps[ids["missing"]].ok is False and snaps[ids["missing"]].attempts == 3
        good_sha = git(repos["good"], "rev-parse", "HEAD").strip()
        assert s.get(Submission, ids["good"]).frozen_sha == good_sha

    found = cross_team_checks()
    assert found["identical_code"] == 2  # good <-> copy, both directions
    decisions = apply_r1_decisions()
    assert decisions["hard_fail"] == 1  # "few" (missing repo is only flagged: no API confirmation)

    with db.session() as s:
        sub = {n: s.get(Submission, i) for n, i in ids.items()}
        assert sub["good"].status == "active" and sub["good"].round == 2
        assert sub["few"].status == "disqualified"
        assert "published minimum is 5" in sub["few"].status_reason
        rules = {c.rule: c.result for c in s.scalars(select(Check).where(Check.submission_id == ids["dump"]))}
        assert rules["first_commit_dump"] == "flag"
        old = {c.rule: c.result for c in s.scalars(select(Check).where(Check.submission_id == ids["old"]))}
        assert old["commits_before_start"] == "flag"
        missing = {c.rule: c.result for c in s.scalars(select(Check).where(Check.submission_id == ids["missing"]))}
        assert missing["repo_accessible"] == "flag"
        assert sub["missing"].status == "active"  # flags never disqualify on their own
        notice = s.scalars(select(Notice).where(Notice.submission_id == ids["few"])).one()
        assert Path(notice.body_path).exists() and "appeal" in notice.links
        html = Path(notice.body_path).read_text()
        assert "min_commits" in html and "File an appeal" in html
        token = s.scalars(select(Appeal).where(Appeal.submission_id == ids["few"])).one().token

    # Appeal accepted -> reinstated; re-running decisions keeps the reviewer's call.
    a = file_appeal(token, "We squashed commits by mistake; see the original branch.")
    decide_appeal(a.id, True, "reviewer1", "Verified original history")
    apply_r1_decisions()
    with db.session() as s:
        assert s.get(Submission, ids["few"]).status == "active"

    # Upholding a flag disqualifies with evidence.
    with db.session() as s:
        f = s.scalars(select(Flag).where(Flag.submission_id == ids["copy"], Flag.type == "identical_code")).first()
    decide_flag(f.id, "upheld", "reviewer1", "Copied from team good")
    with db.session() as s:
        assert s.get(Submission, ids["copy"]).status == "disqualified"
        assert s.scalars(select(Notice).where(Notice.submission_id == ids["copy"])).first() is not None


def test_rerun_is_idempotent(settings, tmp_path):
    repo = make_repo(tmp_path / "r", [{f"f{i}.py": code_file(i)} for i in range(6)])
    sid = add("t1", repo)
    freeze_heads()
    jobs.drain()
    freeze_heads()  # second call is a no-op for frozen teams
    with db.session() as s:
        jobs.enqueue(s, "r1_rules", str(sid), {"submission_id": sid}, requeue=True)
    jobs.drain()
    with db.session() as s:
        assert len(s.scalars(select(Snapshot).where(Snapshot.submission_id == sid)).all()) == 1
        checks = s.scalars(select(Check).where(Check.submission_id == sid)).all()
        assert len({c.rule for c in checks}) == len(checks)

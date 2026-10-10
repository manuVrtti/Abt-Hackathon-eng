from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hackeval import db
from hackeval.config import EventCfg, Settings, set_settings

ROOT = Path(__file__).resolve().parent.parent
START = datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc)
DEADLINE = START + timedelta(days=2)


# Set HACKEVAL_TEST_DATABASE_URL (e.g. postgresql+psycopg://postgres@/hackeval_test?host=/tmp&port=5499)
# to run the suite against PostgreSQL; tables are dropped and recreated per test.
PG_URL = os.environ.get("HACKEVAL_TEST_DATABASE_URL")


@pytest.fixture
def settings(tmp_path):
    s = Settings(
        database_url=PG_URL or f"sqlite:///{tmp_path}/db.sqlite",
        data_dir=tmp_path / "data",
        tracks_dir=ROOT / "tracks",
        event=EventCfg(name="Test Hack", start=START, deadline=DEADLINE,
                       public_base_url="http://appeals.test"),
    )
    s.snapshot.recheck_after_hours = 0
    s.snapshot.retry_delays_min = [0, 0]
    s.tester.warmup_timeout_s = 5
    s.tester.request_timeout_s = 5
    s.llm.provider = "mock"
    set_settings(s)
    db.reset_engines()
    if PG_URL:
        from hackeval.models import Base

        Base.metadata.drop_all(db.engine())
    db.init_db()
    yield s
    db.reset_engines()
    set_settings(None)


def git(repo: Path, *args: str, when: datetime | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
           "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com"}
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.isoformat()
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=env).stdout


def make_repo(path: Path, commits: list[dict[str, str]], start: datetime = START + timedelta(hours=1),
              step: timedelta = timedelta(hours=2)) -> Path:
    """Create a repo where each commit writes the given {path: content} files."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for i, files in enumerate(commits):
        for rel, content in files.items():
            f = path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"commit {i}", when=start + i * step)
    return path


def code_file(seed: int, lines: int = 30) -> str:
    out = [f"def handler_{seed}_{i}(request, value):\n    total = value * {seed + i}\n"
           f"    if total > {i}:\n        return {{'ok': True, 'n': total + {i}}}\n    return None\n"
           for i in range(lines)]
    return "\n".join(out)

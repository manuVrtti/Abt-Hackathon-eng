"""Problem-statement tracks. Each track is a folder in tracks_dir:

    tracks/<track_id>/track.yaml        name, problem statement, test files
    tracks/<track_id>/public_tests.py   published sample (self-check kit)
    tracks/<track_id>/hidden_tests.py   hidden contract tests (keep private)

Keep the real hidden tests outside the public repository and point
``tracks_dir`` at them on the server.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from .config import get_settings
from .livetest.framework import ContractTest, load_suite


@dataclass
class Track:
    id: str
    name: str
    problem_statement: str
    dir: Path
    hidden_tests: Path
    public_tests: Path | None
    warmup_path: str = "/"
    rubric_path: Path | None = None

    def load_hidden(self) -> list[ContractTest]:
        return _load(str(self.hidden_tests))

    def load_public(self) -> list[ContractTest]:
        return _load(str(self.public_tests)) if self.public_tests else []


@lru_cache(maxsize=64)
def _load(path: str) -> list[ContractTest]:
    return load_suite(Path(path))


def get_track(track_id: str) -> Track:
    base = get_settings().tracks_dir / track_id
    spec = base / "track.yaml"
    if not spec.exists():
        raise KeyError(f"track '{track_id}' not found in {get_settings().tracks_dir}")
    d = yaml.safe_load(spec.read_text()) or {}
    pub = d.get("public_tests", "public_tests.py")
    return Track(
        id=track_id,
        name=d.get("name", track_id),
        problem_statement=d.get("problem_statement", ""),
        dir=base,
        hidden_tests=base / d.get("hidden_tests", "hidden_tests.py"),
        public_tests=(base / pub) if pub and (base / pub).exists() else None,
        warmup_path=d.get("warmup_path", "/"),
        rubric_path=(base / d["rubric"]) if d.get("rubric") else None,
    )


def list_tracks() -> list[str]:
    root = get_settings().tracks_dir
    return sorted(p.name for p in root.iterdir() if (p / "track.yaml").exists()) if root.exists() else []

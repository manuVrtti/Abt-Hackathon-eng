"""Git history analysis that needs only commits and trees, never file contents.

Works on the blob-less partial clones taken at the deadline, so it is cheap
for all 10,000 repos. (The plan lists PyDriller; plain git plumbing gives the
same facts without fetching blobs, which PyDriller would trigger.)
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .config import RulesCfg
from .util import run


# GIT_NO_LAZY_FETCH stops a blob-less clone from silently downloading content.
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1",
           "GIT_ASKPASS": "/bin/true"}


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, timeout: float = 300) -> str:
    r = run(["git", "-C", str(repo), *args], timeout=timeout, env=GIT_ENV)
    if r.code != 0:
        raise GitError(f"git {' '.join(args)} failed: {r.err.strip()[:500]}")
    return r.out


def is_code_path(path: str, cfg: RulesCfg) -> bool:
    p = PurePosixPath(path)
    if any(part in cfg.ignore_dirs for part in p.parts[:-1]):
        return False
    if p.name in cfg.ignore_files or p.name.endswith((".min.js", ".min.css", ".map")):
        return False
    return p.suffix.lower() in cfg.code_extensions


@dataclass
class Commit:
    sha: str
    author_time: int
    commit_time: int
    author_email: str
    parents: list[str]


@dataclass
class HistoryStats:
    head: str
    tree: str
    commit_count: int
    root_commits: list[str]
    authors: int
    first_author_time: datetime | None
    last_author_time: datetime | None
    head_commit_time: datetime | None
    active_days: int
    active_hours: int
    sessions: int
    commits_before_start: int
    commits_after_deadline: int
    final_code_files: int
    first_commit_code_share: float
    largest_commit_share: float
    largest_commit_sha: str | None
    code_blobs: list[str] = field(repr=False, default_factory=list)

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("code_blobs")
        for k in ("first_author_time", "last_author_time", "head_commit_time"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


def _ts(v: int) -> datetime:
    return datetime.fromtimestamp(v, tz=timezone.utc)


def list_commits(repo: Path, sha: str) -> list[Commit]:
    out = git(repo, "log", "--format=%H%x1f%at%x1f%ct%x1f%ae%x1f%P", sha)
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        h, at, ct, ae, parents = line.split("\x1f")
        commits.append(Commit(h, int(at), int(ct), ae.lower(), parents.split()))
    return commits


def final_tree(repo: Path, sha: str, cfg: RulesCfg) -> dict[str, str]:
    """path -> blob oid for code files in the commit's tree."""
    out = git(repo, "ls-tree", "-r", "--full-tree", sha)
    files = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        _mode, typ, oid = meta.split()
        if typ == "blob" and is_code_path(path, cfg):
            files[path] = oid
    return files


def blob_introductions(repo: Path, sha: str) -> dict[str, str]:
    """blob oid -> commit that first introduced it (oldest first)."""
    out = git(repo, "log", "--raw", "--no-renames", "--no-abbrev", "--reverse",
              "--format=%x1e%H", sha, timeout=600)
    first: dict[str, str] = {}
    current = None
    # split("\n"), not splitlines(): splitlines() also breaks on the \x1e marker.
    for line in out.split("\n"):
        if line.startswith("\x1e"):
            current = line[1:].strip()
        elif line.startswith(":") and current:
            meta = line.split("\t", 1)[0].split()
            if len(meta) >= 5:
                new_oid = meta[3]
                if set(new_oid) != {"0"}:
                    first.setdefault(new_oid, current)
    return first


def analyse(repo: Path, sha: str, cfg: RulesCfg, start: datetime, deadline: datetime,
            session_gap_s: int = 3600) -> HistoryStats:
    commits = list_commits(repo, sha)
    if not commits:
        raise GitError("no commits")
    tree = git(repo, "rev-parse", f"{sha}^{{tree}}").strip()
    roots = [c.sha for c in commits if not c.parents]
    times = sorted(c.author_time for c in commits)
    sessions = 1 + sum(1 for a, b in zip(times, times[1:]) if b - a > session_gap_s)

    files = final_tree(repo, sha, cfg)
    intro = blob_introductions(repo, sha)
    per_commit: dict[str, int] = defaultdict(int)
    for oid in files.values():
        c = intro.get(oid)
        if c:
            per_commit[c] += 1
    n = len(files)
    root_share = sum(per_commit[r] for r in roots) / n if n else 0.0
    if per_commit:
        largest_sha, largest = max(per_commit.items(), key=lambda kv: kv[1])
    else:
        largest_sha, largest = None, 0

    start_ts, deadline_ts = start.timestamp(), deadline.timestamp()
    head = commits[0]
    return HistoryStats(
        head=sha, tree=tree, commit_count=len(commits), root_commits=roots,
        authors=len({c.author_email for c in commits}),
        first_author_time=_ts(times[0]), last_author_time=_ts(times[-1]),
        head_commit_time=_ts(head.commit_time),
        active_days=len({_ts(t).date() for t in times}),
        active_hours=len({t // 3600 for t in times}),
        sessions=sessions,
        commits_before_start=sum(1 for c in commits if c.author_time < start_ts),
        commits_after_deadline=sum(1 for c in commits if c.commit_time > deadline_ts),
        final_code_files=n,
        first_commit_code_share=round(root_share, 4),
        largest_commit_share=round(largest / n, 4) if n else 0.0,
        largest_commit_sha=largest_sha,
        code_blobs=sorted(set(files.values())),
    )

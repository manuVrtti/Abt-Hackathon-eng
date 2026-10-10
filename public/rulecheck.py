#!/usr/bin/env python3
"""Hackathon rule self-check. Run it on your own repository at any time:

    python rulecheck.py /path/to/your/repo [--rules published_rules.json]

It runs the same objective R1 checks the engine runs (minimum commits, first
commit size, dates, size) on your local clone, plus GitHub's server-side
creation date if the repository has a github.com remote. Standard library
only; needs git on your PATH. It does not send your code anywhere.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

DEFAULT_RULES = {"min_commits": 5, "first_commit_share_flag": 0.7, "repo_size_limit_mb": 200,
                 "start": None, "deadline": None}
CODE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs", ".rb", ".php", ".cs", ".c", ".cc",
            ".cpp", ".h", ".hpp", ".swift", ".dart", ".scala", ".vue", ".svelte", ".html", ".css", ".scss", ".sql",
            ".sh"}
IGNORE_DIRS = {"node_modules", "vendor", "dist", "build", ".next", "out", "venv", ".venv", "env", "__pycache__",
               "target", ".git", "coverage", ".expo", "Pods", "bower_components"}
IGNORE_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Cargo.lock",
                "composer.lock", "go.sum"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def is_code(path: str) -> bool:
    p = PurePosixPath(path)
    return (not any(x in IGNORE_DIRS for x in p.parts[:-1]) and p.name not in IGNORE_FILES
            and not p.name.endswith((".min.js", ".min.css")) and p.suffix.lower() in CODE_EXT)


def parse_dt(v):
    if not v:
        return None
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def github_created_at(repo: Path):
    try:
        url = git(repo, "remote", "get-url", "origin").strip()
    except subprocess.CalledProcessError:
        return None, "no origin remote"
    m = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        return None, "origin is not on github.com"
    req = urllib.request.Request(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "rulecheck"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        return data, None
    except Exception as e:  # noqa: BLE001
        return None, f"GitHub API: {e} (the repository must be public)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--rules", help="published_rules.json from the participant kit")
    ap.add_argument("--branch", default="HEAD")
    a = ap.parse_args()
    rules = dict(DEFAULT_RULES)
    if a.rules:
        rules.update(json.loads(Path(a.rules).read_text()))
    repo = Path(a.repo)
    start, deadline = parse_dt(rules.get("start")), parse_dt(rules.get("deadline"))
    results = []

    def report(name, status, msg):
        results.append(status)
        print(f"[{status:<4}] {name}: {msg}")

    try:
        commits = git(repo, "log", "--format=%H %at", a.branch).split("\n")
    except subprocess.CalledProcessError as e:
        print(f"Cannot read git history: {e.stderr}")
        return 2
    commits = [c.split() for c in commits if c.strip()]
    n = len(commits)
    report("min_commits", "PASS" if n >= rules["min_commits"] else "FAIL",
           f"{n} commits on {a.branch} (minimum {rules['min_commits']})")

    tree = {}
    for line in git(repo, "ls-tree", "-r", "--full-tree", a.branch).splitlines():
        meta, path = line.split("\t", 1)
        _, typ, oid = meta.split()
        if typ == "blob" and is_code(path):
            tree[path] = oid
    roots = set(git(repo, "rev-list", "--max-parents=0", a.branch).split())
    first, current = {}, None
    for line in git(repo, "log", "--raw", "--no-renames", "--no-abbrev", "--reverse", "--format=\x1e%H",
                    a.branch).split("\n"):  # not splitlines(): it splits on \x1e too
        if line.startswith("\x1e"):
            current = line[1:].strip()
        elif line.startswith(":") and current:
            parts = line.split("\t", 1)[0].split()
            if len(parts) >= 5 and set(parts[3]) != {"0"}:
                first.setdefault(parts[3], current)
    share = sum(1 for oid in tree.values() if first.get(oid) in roots) / len(tree) if tree else 0
    flag = len(tree) >= 3 and share > rules["first_commit_share_flag"]
    report("first_commit_dump", "FLAG" if flag else "PASS",
           f"first commit contains {share:.0%} of your final code files "
           f"(flagged for human review above {rules['first_commit_share_flag']:.0%})")

    if start:
        early = sum(1 for _, t in commits if int(t) < start.timestamp())
        report("commits_before_start", "FLAG" if early else "PASS", f"{early} commits dated before the start")
    data, err = github_created_at(repo)
    if data:
        created = parse_dt(data["created_at"])
        if start:
            report("repo_created_after_start", "PASS" if created >= start else "FAIL",
                   f"GitHub says the repo was created {created.isoformat()}")
        report("public", "PASS" if not data.get("private") else "FAIL", "repository is public")
        report("fork", "FLAG" if data.get("fork") else "PASS",
               "repository is a fork (flagged for review)" if data.get("fork") else "not a fork")
        size_mb = data.get("size", 0) / 1024
        report("repo_size", "FLAG" if size_mb > rules["repo_size_limit_mb"] else "PASS",
               f"{size_mb:.0f} MB on GitHub (limit {rules['repo_size_limit_mb']} MB)")
    else:
        report("github", "INFO", err)
    if deadline:
        print(f"\nOnly the commit at the head of your default branch at {deadline.isoformat()} is judged.")
    print("\nFAIL = would be disqualified · FLAG = a person reviews it · nothing is final until the deadline.")
    return 1 if "FAIL" in results else 0


if __name__ == "__main__":
    sys.exit(main())

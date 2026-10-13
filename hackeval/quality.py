"""R2 code quality (15%): static analysis, README, leaked secrets, hygiene.

External tools (semgrep, ruff, eslint, gitleaks, scc) are used when
installed. A missing tool is recorded in the score detail and its share of
the score is skipped for everyone alike, except secrets and line counts,
which have built-in fallbacks.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from pathlib import Path

from sqlalchemy import select

from . import ENGINE_VERSION, db, jobs
from .config import get_settings
from .gitstats import is_code_path
from .models import Score, Submission
from .snapshot import code_path
from .util import run

SECRET_PATTERNS = {
    "aws_access_key": r"AKIA[0-9A-Z]{16}",
    "github_token": r"gh[pousr]_[A-Za-z0-9]{36,}",
    "openai_key": r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}",
    "anthropic_key": r"sk-ant-[A-Za-z0-9_-]{32,}",
    "google_api_key": r"AIza[0-9A-Za-z_-]{35}",
    "slack_token": r"xox[baprs]-[0-9A-Za-z-]{10,}",
    "stripe_secret": r"sk_live_[0-9a-zA-Z]{24,}",
    "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "mongodb_uri_with_password": r"mongodb(?:\+srv)?://[^:\s/]+:[^@\s]+@",
    "postgres_uri_with_password": r"postgres(?:ql)?://[^:\s/]+:[^@\s]+@",
}


def iter_code_files(root: Path):
    cfg = get_settings().rules
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in cfg.ignore_dirs]
        for f in filenames:
            p = Path(dirpath) / f
            rel = p.relative_to(root).as_posix()
            if is_code_path(rel, cfg):
                yield p, rel


def count_loc(root: Path) -> tuple[int, dict]:
    if shutil.which("scc"):
        r = run(["scc", "--format", "json", "--exclude-dir", ",".join(get_settings().rules.ignore_dirs), str(root)],
                timeout=300)
        if r.code == 0:
            try:
                langs = json.loads(r.out)
                return sum(l["Code"] for l in langs), {l["Name"]: l["Code"] for l in langs}
            except (ValueError, KeyError):
                pass
    total, by_ext = 0, {}
    for p, rel in iter_code_files(root):
        try:
            n = sum(1 for line in p.open(errors="ignore") if line.strip())
        except OSError:
            continue
        total += n
        by_ext[p.suffix] = by_ext.get(p.suffix, 0) + n
    return total, by_ext


def readme_score(root: Path, live_url: str) -> tuple[float, dict]:
    candidates = [p for p in root.iterdir() if p.is_file() and p.name.lower().startswith("readme")] if root.exists() else []
    if not candidates:
        return 0.0, {"found": False}
    text = max(candidates, key=lambda p: p.stat().st_size).read_text(errors="ignore")
    words = len(re.findall(r"\w+", text))
    headings = len(re.findall(r"^#{1,6}\s", text, re.M))
    low = text.lower()
    checks = {
        "length": min(words / 300, 1.0),
        "structure": min(headings / 4, 1.0),
        "setup": 1.0 if re.search(r"install|setup|getting started|run locally|how to run|usage", low) else 0.0,
        "tech_stack": 1.0 if re.search(r"tech stack|built with|technolog|architecture|stack", low) else 0.0,
        "live_link": 1.0 if live_url and urlish(live_url) in low else 0.0,
    }
    w = {"length": 0.3, "structure": 0.2, "setup": 0.25, "tech_stack": 0.15, "live_link": 0.1}
    return round(100 * sum(checks[k] * w[k] for k in w), 2), {"found": True, "words": words, "headings": headings,
                                                              **{f"has_{k}": v for k, v in checks.items()}}


def urlish(u: str) -> str:
    return re.sub(r"^https?://", "", u.lower()).rstrip("/")


def lint_issues(root: Path) -> tuple[float | None, dict]:
    """Weighted issue count from available linters, or None if none ran."""
    detail: dict = {"tools": {}}
    weighted = 0.0
    ran = False
    exts = {p.suffix for p, _ in iter_code_files(root)}
    if shutil.which("ruff") and ".py" in exts:
        r = run(["ruff", "check", "--output-format", "json", "--exit-zero", "--no-cache", "--isolated",
                 "--select", "E9,F,B,PLE", str(root)], timeout=300)
        try:
            items = json.loads(r.out or "[]")
            ignored = set(get_settings().rules.ignore_dirs)
            items = [i for i in items if not ignored & set(Path(i.get("filename", "")).parts)]
            weighted += len(items)
            detail["tools"]["ruff"] = len(items)
            ran = True
        except ValueError:
            detail["tools"]["ruff"] = "error"
    if shutil.which("eslint") and exts & {".js", ".jsx", ".mjs"}:
        rules = {"no-unreachable": "error", "no-dupe-keys": "error", "no-dupe-args": "error",
                 "no-func-assign": "error", "no-self-compare": "error", "no-unsafe-finally": "error",
                 "no-unused-vars": "warn", "no-empty": "warn", "no-constant-condition": "warn"}
        cmd = ["eslint", "--no-config-lookup", "--format", "json", "--no-warn-ignored",
               "--parser-options", "ecmaVersion:latest", "--parser-options", "sourceType:module",
               "--parser-options", "ecmaFeatures:{jsx:true}"]
        for k, v in rules.items():
            cmd += ["--rule", json.dumps({k: v})]
        for d in get_settings().rules.ignore_dirs:
            cmd += ["--ignore-pattern", f"**/{d}/**"]
        r = run(cmd + ["**/*.js", "**/*.jsx", "**/*.mjs"], cwd=root, timeout=600)
        try:
            files = json.loads(r.out or "[]")
            errs = sum(f.get("errorCount", 0) for f in files)
            warns = sum(f.get("warningCount", 0) for f in files)
            fatal = sum(f.get("fatalErrorCount", 0) for f in files)
            weighted += errs + 0.5 * warns
            detail["tools"]["eslint"] = {"errors": errs, "warnings": warns, "parse_errors": fatal}
            ran = True
        except ValueError:
            detail["tools"]["eslint"] = "error: " + r.err[-200:]
    semgrep_cfg = os.environ.get("HACKEVAL_SEMGREP_CONFIG", "p/default")
    if shutil.which("semgrep"):
        r = run(["semgrep", "scan", "--config", semgrep_cfg, "--json", "--metrics=off", "--quiet",
                 "--timeout", "30", str(root)], timeout=900)
        try:
            res = json.loads(r.out or "{}").get("results", [])
            sev = {"ERROR": 3, "WARNING": 1, "INFO": 0.25}
            weighted += sum(sev.get(x.get("extra", {}).get("severity", "INFO"), 0.25) for x in res)
            detail["tools"]["semgrep"] = len(res)
            ran = True
        except ValueError:
            detail["tools"]["semgrep"] = "error"
    return (weighted if ran else None), detail


def secrets_scan(root: Path) -> tuple[list[dict], str]:
    if shutil.which("gitleaks"):
        r = run(["gitleaks", "dir", str(root), "--report-format", "json", "--report-path", "/dev/stdout",
                 "--no-banner", "--exit-code", "0"], timeout=600)
        try:
            items = json.loads(r.out or "[]")
            return [{"rule": i.get("RuleID"), "file": i.get("File", "").replace(str(root) + "/", ""),
                     "line": i.get("StartLine")} for i in items], "gitleaks"
        except ValueError:
            pass
    found = []
    pats = {k: re.compile(v) for k, v in SECRET_PATTERNS.items()}
    ignore = set(get_settings().rules.ignore_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for f in filenames:
            p = Path(dirpath) / f
            try:
                if p.stat().st_size > 1_000_000:
                    continue
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            for name, pat in pats.items():
                m = pat.search(text)
                if m:
                    found.append({"rule": name, "file": p.relative_to(root).as_posix(),
                                  "line": text.count("\n", 0, m.start()) + 1})
    return found, "builtin"


def hygiene_score(root: Path) -> tuple[float, dict]:
    names = {p.name for p in root.rglob("*") if ".git" not in p.parts} if root.exists() else set()
    top = {p.name for p in root.iterdir()} if root.exists() else set()
    committed_junk = [d for d in ("node_modules", "venv", ".venv", "__pycache__", "dist", ".next")
                      if any(root.rglob(d))]
    env_files = [p.relative_to(root).as_posix() for p in root.rglob(".env") if p.is_file()]
    has_tests = any(n in names for n in ("tests", "test", "__tests__", "spec")) or any(
        re.search(r"(_test|\.test|\.spec|^test_)\.?\w*\.\w+$", n) for n in names)
    manifest = bool(top & {"package.json", "requirements.txt", "pyproject.toml", "go.mod", "pom.xml",
                           "build.gradle", "Cargo.toml", "composer.json", "Gemfile", "pubspec.yaml"})
    checks = {"gitignore": ".gitignore" in top, "no_committed_dependencies": not committed_junk,
              "no_env_file": not env_files, "has_tests": has_tests, "dependency_manifest": manifest}
    w = {"gitignore": 0.15, "no_committed_dependencies": 0.25, "no_env_file": 0.2, "has_tests": 0.25,
         "dependency_manifest": 0.15}
    return round(100 * sum(w[k] for k, v in checks.items() if v), 2), {
        **checks, "committed_dirs": committed_junk, "env_files": env_files[:10]}


def assess(root: Path, live_url: str = "") -> tuple[float, dict]:
    weights = {k: v for k, v in get_settings().quality.parts.items() if v > 0}
    loc, langs = count_loc(root)
    kloc = max(loc / 1000, 0.2)
    parts: dict[str, float] = {}
    detail: dict = {"loc": loc, "languages": langs}

    if "readme" in weights:
        parts["readme"], detail["readme"] = readme_score(root, live_url)
    if "static" in weights:
        weighted, lint = lint_issues(root)
        detail["static"] = lint
        if weighted is not None:
            density = weighted / kloc
            detail["static"]["issues_per_kloc"] = round(density, 2)
            parts["static"] = round(100 * math.exp(-density / 15), 2)
    if "secrets" in weights:
        secrets, tool = secrets_scan(root)
        parts["secrets"] = 0.0 if secrets else 100.0
        detail["secrets"] = {"tool": tool, "count": len(secrets), "findings": secrets[:20]}
    if "hygiene" in weights:
        parts["hygiene"], detail["hygiene"] = hygiene_score(root)
    if loc == 0:
        detail["note"] = "no code files found"
    total_w = sum(weights[k] for k in parts) or 1
    value = sum(parts[k] * weights[k] for k in parts) / total_w
    if loc == 0:
        value = min(value, 10.0)
    detail["parts"] = parts
    detail["weights_used"] = {k: weights[k] for k in parts}
    return round(value, 2), detail


@jobs.handler("quality")
def quality_job(p: dict) -> dict:
    sub_id = p["submission_id"]
    with db.session() as s:
        root = code_path(s, sub_id)
        sub = s.get(Submission, sub_id)
        live = sub.live_url
    if root is None:
        raise jobs.Retry(300, "full code not fetched yet")
    value, detail = assess(root, live)
    with db.session() as s:
        set_score(s, sub_id, "quality", value, detail)
    return {"quality": value}


def set_score(s, sub_id: int, component: str, value: float, detail: dict) -> Score:
    sc = s.scalars(select(Score).where(Score.submission_id == sub_id, Score.component == component)).first()
    if sc is None:
        sc = Score(submission_id=sub_id, component=component)
        s.add(sc)
    sc.value, sc.detail, sc.engine_version = value, detail, ENGINE_VERSION
    return sc

from __future__ import annotations

import hashlib
import re
import secrets
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; treat naive datetimes from the DB as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def new_token() -> str:
    return secrets.token_urlsafe(24)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_GH_RE = re.compile(r"^(?:https?://|ssh://)?(?:git@)?(?:www\.)?github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


def parse_github_url(url: str) -> tuple[str, str] | None:
    """Return (owner, repo) for a GitHub URL, else None. Tolerates .git and /tree/... suffixes."""
    m = _GH_RE.match(url.strip())
    if not m:
        return None
    repo = re.sub(r"\.git$", "", m.group(2))
    return (m.group(1), repo) if repo else None


def normalize_live_url(url: str) -> str:
    url = url.strip()
    if url and not re.match(r"^https?://", url):
        url = "https://" + url
    return url.rstrip("/")


@dataclass
class CmdResult:
    code: int
    out: str
    err: str


def run(cmd: list[str], cwd: Path | str | None = None, timeout: float | None = None,
        env: dict | None = None, input: str | None = None) -> CmdResult:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env=env, input=input, errors="replace")
        return CmdResult(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired as e:
        return CmdResult(124, (e.stdout or "") if isinstance(e.stdout, str) else "", f"timeout after {timeout}s")
    except FileNotFoundError as e:
        return CmdResult(127, "", str(e))


def truncate(s: str | None, n: int) -> str | None:
    if s is None or len(s) <= n:
        return s
    return s[:n] + f"\n...[truncated {len(s) - n} chars]"

"""Import submissions exported by the hackathon website as CSV."""

from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from . import db
from .models import Submission
from .util import normalize_live_url, parse_github_url

ALIASES = {
    "team_id": ["team_id", "teamid", "team id", "id", "submission_id"],
    "team_name": ["team_name", "team", "name", "team name"],
    "email": ["email", "contact_email", "leader_email", "e-mail"],
    "track": ["track", "problem_statement", "problem", "ps", "theme"],
    "repo_url": ["repo_url", "repo", "github", "github_url", "repository", "repository_url"],
    "live_url": ["live_url", "live", "live_link", "deployed_url", "demo_url", "url", "deployment"],
    "submitted_at": ["submitted_at", "submission_time", "timestamp", "created_at"],
}


def _norm(h: str) -> str:
    return re.sub(r"[\s\-]+", "_", h.strip().lower())


def _resolve_columns(header: list[str]) -> dict[str, str]:
    lower = {_norm(h): h for h in header}
    out = {}
    for field, names in ALIASES.items():
        for n in names:
            if _norm(n) in lower:
                out[field] = lower[_norm(n)]
                break
    missing = {"team_id", "repo_url"} - out.keys()
    if missing:
        raise ValueError(f"CSV is missing required columns: {sorted(missing)} (got {header})")
    return out


def _parse_time(v: str) -> datetime | None:
    v = (v or "").strip()
    if not v:
        return None
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00")) if fmt is None else datetime.strptime(v, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def import_csv(path: Path, default_track: str = "default", update: bool = False) -> dict:
    created = updated = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = _resolve_columns(reader.fieldnames or [])
        with db.session() as s:
            existing = {sub.team_id: sub for sub in s.scalars(select(Submission))}
            for row in reader:
                get = lambda k: (row.get(cols[k]) or "").strip() if k in cols else ""  # noqa: E731
                team_id = get("team_id")
                if not team_id:
                    skipped += 1
                    continue
                repo_url = get("repo_url")
                parsed = parse_github_url(repo_url)
                fields = dict(
                    team_name=get("team_name"),
                    email=get("email"),
                    track=get("track") or default_track,
                    repo_url=repo_url,
                    repo_owner=parsed[0] if parsed else "",
                    repo_name=parsed[1] if parsed else "",
                    live_url=normalize_live_url(get("live_url")),
                    submitted_at=_parse_time(get("submitted_at")),
                )
                sub = existing.get(team_id)
                if sub is None:
                    sub = Submission(team_id=team_id, **fields)
                    s.add(sub)
                    existing[team_id] = sub
                    created += 1
                elif update and sub.round == 0:
                    for k, v in fields.items():
                        setattr(sub, k, v)
                    updated += 1
                else:
                    skipped += 1
    return {"created": created, "updated": updated, "skipped": skipped}

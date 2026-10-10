"""GitHub GraphQL client: server-side repo facts for ~100 repos per call."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

log = logging.getLogger("hackeval.github")

GRAPHQL_URL = "https://api.github.com/graphql"
FIELDS = """
  nameWithOwner createdAt pushedAt isFork isPrivate isArchived diskUsage
  parent { nameWithOwner }
  defaultBranchRef { name target { oid } }
"""


@dataclass
class RepoFacts:
    owner: str
    name: str
    exists: bool
    is_private: bool | None = None
    is_fork: bool | None = None
    parent: str | None = None
    created_at: datetime | None = None
    pushed_at: datetime | None = None
    disk_usage_kb: int | None = None
    default_branch: str | None = None
    head_sha: str | None = None
    raw: dict | None = None
    error: str | None = None


def _q(s: str) -> str:
    return json.dumps(s)


def build_query(repos: list[tuple[str, str]]) -> str:
    parts = [f"r{i}: repository(owner: {_q(o)}, name: {_q(n)}) {{{FIELDS}}}" for i, (o, n) in enumerate(repos)]
    return "query {\n" + "\n".join(parts) + "\n rateLimit { remaining resetAt cost }\n}"


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


def parse_response(repos: list[tuple[str, str]], body: dict) -> list[RepoFacts]:
    data = body.get("data") or {}
    errors = {}
    for e in body.get("errors") or []:
        path = e.get("path") or []
        if path:
            errors[path[0]] = f"{e.get('type', 'ERROR')}: {e.get('message', '')}"
    out = []
    for i, (owner, name) in enumerate(repos):
        node = data.get(f"r{i}")
        if node is None:
            err = errors.get(f"r{i}", "not returned")
            # NOT_FOUND covers both missing and private-to-us repositories.
            out.append(RepoFacts(owner, name, exists=False, error=err))
            continue
        ref = node.get("defaultBranchRef") or {}
        out.append(RepoFacts(
            owner=owner, name=name, exists=True,
            is_private=node.get("isPrivate"), is_fork=node.get("isFork"),
            parent=(node.get("parent") or {}).get("nameWithOwner"),
            created_at=_dt(node.get("createdAt")), pushed_at=_dt(node.get("pushedAt")),
            disk_usage_kb=node.get("diskUsage"),
            default_branch=ref.get("name"), head_sha=(ref.get("target") or {}).get("oid"),
            raw=node,
        ))
    return out


async def fetch_batch(client: httpx.AsyncClient, token: str, repos: list[tuple[str, str]],
                      retries: int = 4) -> list[RepoFacts]:
    query = build_query(repos)
    delay = 5.0
    for attempt in range(retries):
        try:
            r = await client.post(GRAPHQL_URL, json={"query": query},
                                  headers={"Authorization": f"bearer {token}"}, timeout=60)
            if r.status_code in (502, 503, 504) or r.status_code == 403 and "rate" in r.text.lower():
                raise httpx.HTTPStatusError("retryable", request=r.request, response=r)
            r.raise_for_status()
            return parse_response(repos, r.json())
        except (httpx.HTTPError, ValueError) as e:
            log.warning("GraphQL batch failed (attempt %d): %s", attempt + 1, e)
            if attempt == retries - 1:
                return [RepoFacts(o, n, exists=False, error=f"api_error: {e}") for o, n in repos]
            await asyncio.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


async def fetch_all(token: str, repos: list[tuple[str, str]], batch_size: int = 100,
                    concurrency: int = 4, transport: httpx.AsyncBaseTransport | None = None) -> list[RepoFacts]:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(transport=transport) as client:
        async def one(chunk):
            async with sem:
                return await fetch_batch(client, token, chunk)

        chunks = [repos[i:i + batch_size] for i in range(0, len(repos), batch_size)]
        results = await asyncio.gather(*(one(c) for c in chunks))
    return [f for chunk in results for f in chunk]

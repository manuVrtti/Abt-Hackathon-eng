"""Public "ping my backend" check, open until the deadline.

A team enters its live URL; the tester sends one GET from the event server
with the published User-Agent and an X-Eval-Request-ID, and shows the team
the request ID, time, status and the server IP it reached. The team can then
find that exact request in its own logs, so "your tester never reached us"
can be settled before the deadline instead of after it.

Because this fetches a URL chosen by the public, it only accepts http(s)
URLs whose host resolves to public internet addresses, does not follow
redirects, reads at most 4 KB and is rate-limited per visitor and per host.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from ..config import get_settings
from ..livetest.framework import ERROR_KINDS, _peer_ip, classify_error
from ..util import normalize_live_url, utcnow

PER_VISITOR = (10, 3600)  # 10 pings per hour per visitor IP
PER_HOST = (30, 3600)     # 30 pings per hour per target host
_hits: dict[str, deque] = defaultdict(deque)


class PingRefused(ValueError):
    pass


@dataclass
class PingResult:
    url: str
    request_id: str
    sent_at: str
    tester_ip: str
    user_agent: str
    header: str
    status_code: int | None = None
    latency_ms: float | None = None
    remote_ip: str | None = None
    error: str | None = None


def _limited(key: str, limit: tuple[int, int]) -> bool:
    n, window = limit
    q = _hits[key]
    now = time.monotonic()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= n:
        return True
    q.append(now)
    return False


async def check_public_url(url: str) -> str:
    url = normalize_live_url(url)
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise PingRefused("Enter a full http:// or https:// address.")
    if p.username or p.password:
        raise PingRefused("Addresses with a username or password are not allowed.")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        raise PingRefused(f"The domain {p.hostname} does not resolve (DNS). Check the address.") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise PingRefused("Only public internet addresses can be checked.")
    return url


async def ping(url: str, visitor: str, transport: httpx.AsyncBaseTransport | None = None) -> PingResult:
    cfg = get_settings()
    if not cfg.tester.ping_page_enabled or utcnow() > cfg.event.deadline:
        raise PingRefused("The ping check is closed: it is only open until the submission deadline.")
    if transport is None:  # tests inject a transport and skip the DNS guard
        url = await check_public_url(url)
    host = urlparse(url).hostname or ""
    if _limited(f"v:{visitor}", PER_VISITOR):
        raise PingRefused("Too many checks from your address. Try again in an hour.")
    if _limited(f"h:{host}", PER_HOST):
        raise PingRefused("This host has been checked many times this hour. Try again later.")
    rid = uuid.uuid4().hex
    res = PingResult(url=url, request_id=rid, sent_at=utcnow().isoformat(), tester_ip=cfg.tester.published_ip,
                     user_agent=cfg.tester.user_agent, header=cfg.tester.request_id_header)
    headers = {"User-Agent": cfg.tester.user_agent, cfg.tester.request_id_header: rid}
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(follow_redirects=False, transport=transport) as client:
            async with client.stream("GET", url, headers=headers, timeout=cfg.tester.warmup_timeout_s) as r:
                res.remote_ip = _peer_ip(r)
                res.status_code = r.status_code
                async for _ in r.aiter_bytes(4096):
                    break
    except httpx.HTTPError as e:
        res.error = ERROR_KINDS[classify_error(e)]
    res.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    return res

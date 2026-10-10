"""Contract test framework.

This file depends only on httpx and is published unchanged in the public
self-check kit, so participants run their sample tests with exactly the code
the engine uses for the hidden ones.

A track's tests are plain async functions:

    from hackeval.livetest.framework import contract_test

    @contract_test(category="basics", weight=1)
    async def health(ctx):
        r = await ctx.get("/health")
        ctx.expect_status(r, 200)

Tests run in file order and can pass data forward through ``ctx.state``.
"""

from __future__ import annotations

import importlib.util
import json as _json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

TestFn = Callable[["Ctx"], Awaitable[None]]


@dataclass
class ContractTest:
    id: str
    fn: TestFn
    category: str
    weight: float
    description: str
    order: int


_counter = 0


def contract_test(category: str = "general", weight: float = 1.0, id: str | None = None,
                  description: str | None = None):
    def deco(fn: TestFn) -> TestFn:
        global _counter
        _counter += 1
        fn.__contract_test__ = ContractTest(  # type: ignore[attr-defined]
            id=id or fn.__name__, fn=fn, category=category, weight=float(weight),
            description=description or (fn.__doc__ or "").strip(), order=_counter)
        return fn

    return deco


class ExpectationFailed(AssertionError):
    pass


@dataclass
class RequestRecord:
    request_id: str
    test_id: str
    method: str
    url: str
    request_body: str | None
    sent_at: datetime
    status_code: int | None = None
    latency_ms: float | None = None
    response_headers: dict | None = None
    response_body: str | None = None
    error: str | None = None


@dataclass
class TestOutcome:
    test_id: str
    category: str
    weight: float
    passed: bool
    message: str
    duration_ms: float


@dataclass
class SuiteResult:
    base_url: str
    reachable: bool
    outcomes: list[TestOutcome] = field(default_factory=list)
    records: list[RequestRecord] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    @property
    def score(self) -> float:
        total = sum(o.weight for o in self.outcomes)
        return round(100 * sum(o.weight for o in self.outcomes if o.passed) / total, 2) if total else 0.0

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed)


@dataclass
class TesterIdentity:
    user_agent: str = "HackEval-Tester/1.0"
    request_id_header: str = "X-Eval-Request-ID"
    warmup_timeout_s: float = 90.0
    request_timeout_s: float = 20.0
    max_body_bytes: int = 65536


class Ctx:
    def __init__(self, base_url: str, client: httpx.AsyncClient, ident: TesterIdentity,
                 records: list[RequestRecord], state: dict):
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.ident = ident
        self.records = records
        self.state = state
        self.test_id = ""

    def url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return self.base_url + ("" if path.startswith("/") else "/") + path

    async def request(self, method: str, path: str, *, json: Any = None, data: Any = None,
                      params: dict | None = None, headers: dict | None = None,
                      timeout: float | None = None) -> httpx.Response:
        rid = uuid.uuid4().hex
        hdrs = {"User-Agent": self.ident.user_agent, self.ident.request_id_header: rid, **(headers or {})}
        body_txt = _json.dumps(json) if json is not None else (str(data) if data is not None else None)
        rec = RequestRecord(rid, self.test_id, method.upper(), self.url(path), body_txt, datetime.now(timezone.utc))
        self.records.append(rec)
        t0 = time.perf_counter()
        try:
            r = await self.client.request(method, self.url(path), json=json, data=data, params=params,
                                          headers=hdrs, timeout=timeout or self.ident.request_timeout_s)
            # Record the URL actually requested (including query string).
            rec.url = str(r.request.url)
        except httpx.HTTPError as e:
            rec.latency_ms = (time.perf_counter() - t0) * 1000
            rec.error = f"{type(e).__name__}: {e}"[:1000]
            raise ExpectationFailed(f"{method.upper()} {path} failed: {type(e).__name__}") from e
        rec.latency_ms = (time.perf_counter() - t0) * 1000
        rec.status_code = r.status_code
        rec.response_headers = {k: v for k, v in r.headers.items()
                                if k.lower() in ("content-type", "content-length", "server", "location")}
        rec.response_body = r.text[: self.ident.max_body_bytes]
        return r

    async def get(self, path: str, **kw) -> httpx.Response:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw) -> httpx.Response:
        return await self.request("POST", path, **kw)

    async def put(self, path: str, **kw) -> httpx.Response:
        return await self.request("PUT", path, **kw)

    async def patch(self, path: str, **kw) -> httpx.Response:
        return await self.request("PATCH", path, **kw)

    async def delete(self, path: str, **kw) -> httpx.Response:
        return await self.request("DELETE", path, **kw)

    # ---- assertions
    def expect(self, cond: Any, message: str) -> None:
        if not cond:
            raise ExpectationFailed(message)

    def expect_status(self, r: httpx.Response, *codes: int) -> None:
        if r.status_code not in codes:
            raise ExpectationFailed(f"{r.request.method} {r.request.url.path} returned {r.status_code}, "
                                    f"expected {' or '.join(map(str, codes))}")

    def expect_json(self, r: httpx.Response) -> Any:
        try:
            return r.json()
        except ValueError:
            raise ExpectationFailed(f"{r.request.method} {r.request.url.path} did not return JSON") from None

    def expect_keys(self, obj: Any, *keys: str) -> None:
        if not isinstance(obj, dict):
            raise ExpectationFailed(f"expected a JSON object, got {type(obj).__name__}")
        missing = [k for k in keys if k not in obj]
        if missing:
            raise ExpectationFailed(f"response is missing fields: {', '.join(missing)}")


def load_suite(path: Path) -> list[ContractTest]:
    """Import a test file and return its tests in declaration order."""
    path = Path(path).resolve()
    name = f"hackeval_suite_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    tests = [v.__contract_test__ for v in vars(mod).values() if hasattr(v, "__contract_test__")]
    ids = [t.id for t in tests]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate test ids in {path}")
    return sorted(tests, key=lambda t: t.order)


async def run_suite(tests: list[ContractTest], base_url: str, ident: TesterIdentity | None = None,
                    client: httpx.AsyncClient | None = None, warmup_path: str = "/") -> SuiteResult:
    ident = ident or TesterIdentity()
    res = SuiteResult(base_url=base_url, reachable=False, started_at=datetime.now(timezone.utc))
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, verify=True)
    state: dict = {}
    ctx = Ctx(base_url, client, ident, res.records, state)
    try:
        # Warm-up: free hosting tiers sleep idle servers, so the first request
        # gets the long timeout. Any HTTP response at all counts as reachable.
        ctx.test_id = "_warmup"
        try:
            await ctx.get(warmup_path, timeout=ident.warmup_timeout_s)
            res.reachable = True
        except ExpectationFailed as e:
            res.error = str(e)
        for t in tests:
            ctx.test_id = t.id
            t0 = time.perf_counter()
            if not res.reachable:
                res.outcomes.append(TestOutcome(t.id, t.category, t.weight, False, "live link unreachable", 0.0))
                continue
            try:
                await t.fn(ctx)
                res.outcomes.append(TestOutcome(t.id, t.category, t.weight, True, "",
                                                (time.perf_counter() - t0) * 1000))
            except ExpectationFailed as e:
                res.outcomes.append(TestOutcome(t.id, t.category, t.weight, False, str(e)[:1000],
                                                (time.perf_counter() - t0) * 1000))
            except Exception as e:  # a bug in a test or an unexpected response shape
                res.outcomes.append(TestOutcome(t.id, t.category, t.weight, False,
                                                f"{type(e).__name__}: {e}"[:1000], (time.perf_counter() - t0) * 1000))
    finally:
        if own:
            await client.aclose()
        res.finished_at = datetime.now(timezone.utc)
    return res


def main(argv: list[str] | None = None) -> int:
    """Self-check entry point: python -m hackeval.livetest.framework TESTS.py URL"""
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(description="Run the public sample tests against your live link.")
    ap.add_argument("tests")
    ap.add_argument("url")
    a = ap.parse_args(argv)
    res = asyncio.run(run_suite(load_suite(Path(a.tests)), a.url))
    print(f"Reachable: {res.reachable}")
    for o in res.outcomes:
        print(f"  [{'PASS' if o.passed else 'FAIL'}] {o.test_id} ({o.category}) {o.message}")
    print(f"Score: {res.score}/100 ({res.passed}/{len(res.outcomes)} tests)")
    return 0 if res.reachable else 1


if __name__ == "__main__":
    raise SystemExit(main())

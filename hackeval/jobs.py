"""Job runner: asyncio workers reading a jobs table (no separate queue service).

Every job has a unique key, so enqueueing the same work twice is a no-op and
every handler is written to be safe to re-run. A crashed worker's jobs are
re-queued once their lock goes stale.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session

from . import db
from .models import Job
from .util import utcnow

log = logging.getLogger("hackeval.jobs")

Handler = Callable[[dict], Any | Awaitable[Any]]
HANDLERS: dict[str, Handler] = {}
STALE_AFTER = timedelta(hours=2)


class Retry(Exception):
    """Raise from a handler to retry later without it counting as a crash."""

    def __init__(self, delay_s: float, reason: str = ""):
        super().__init__(reason or f"retry in {delay_s}s")
        self.delay_s = delay_s


class Permanent(Exception):
    """Raise from a handler to fail the job without further retries."""


def handler(kind: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


def enqueue(s: Session, kind: str, key: str, payload: dict | None = None, *,
            run_after: datetime | None = None, priority: int = 100, max_attempts: int = 3,
            requeue: bool = False) -> Job:
    """Add a job unless one with the same key exists. With requeue=True a
    finished or failed job is reset to run again (used for deliberate re-runs)."""
    full_key = f"{kind}:{key}"
    job = s.scalars(select(Job).where(Job.key == full_key)).first()
    if job is None:
        job = Job(kind=kind, key=full_key, payload=payload or {}, priority=priority,
                  max_attempts=max_attempts, run_after=run_after or utcnow())
        s.add(job)
        s.flush()
    elif requeue and job.status in ("done", "failed"):
        job.status = "queued"
        job.attempts = 0
        job.last_error = None
        job.payload = payload or job.payload
        job.run_after = run_after or utcnow()
    return job


def _claim(worker_id: str, kinds: list[str] | None) -> Job | None:
    now = utcnow()
    with db.session() as s:
        for _ in range(5):
            q = select(Job.id).where(Job.status == "queued", Job.run_after <= now)
            if kinds:
                q = q.where(Job.kind.in_(kinds))
            q = q.order_by(Job.priority, Job.id).limit(1)
            if db.is_postgres():
                q = q.with_for_update(skip_locked=True)
            job_id = s.scalars(q).first()
            if job_id is None:
                return None
            res = s.execute(
                update(Job)
                .where(and_(Job.id == job_id, Job.status == "queued"))
                .values(status="running", locked_by=worker_id, locked_at=now, attempts=Job.attempts + 1)
            )
            if res.rowcount == 1:
                s.commit()
                return s.get(Job, job_id)
            s.rollback()
    return None


def _finish(job_id: int, *, result: Any = None, error: str | None = None,
            retry_delay: float | None = None, permanent: bool = False) -> None:
    with db.session() as s:
        job = s.get(Job, job_id)
        job.locked_by = None
        if error is None:
            job.status = "done"
            job.result = result if isinstance(result, dict) or result is None else {"value": result}
            job.last_error = None
            return
        job.last_error = error[-4000:]
        explicit_retry = retry_delay is not None
        if permanent or (not explicit_retry and job.attempts >= job.max_attempts):
            job.status = "failed"
            return
        if explicit_retry:
            # Explicit retries are part of the handler's design (e.g. clone
            # attempts spread over 30 minutes); they still have a hard ceiling.
            if job.attempts >= max(job.max_attempts, 10):
                job.status = "failed"
                return
        delay = retry_delay if explicit_retry else min(60 * 2 ** (job.attempts - 1), 1800)
        job.status = "queued"
        job.run_after = utcnow() + timedelta(seconds=delay)


def requeue_stale(older_than: timedelta = STALE_AFTER) -> int:
    with db.session() as s:
        res = s.execute(
            update(Job)
            .where(Job.status == "running", Job.locked_at < utcnow() - older_than)
            .values(status="queued", locked_by=None)
        )
        return res.rowcount or 0


async def _run_one(job: Job) -> None:
    fn = HANDLERS.get(job.kind)
    if fn is None:
        await asyncio.to_thread(_finish, job.id, error=f"no handler for {job.kind}", permanent=True)
        return
    payload = {**(job.payload or {}), "_attempt": job.attempts, "_job_id": job.id}
    try:
        if inspect.iscoroutinefunction(fn):
            result = await fn(payload)
        else:
            result = await asyncio.to_thread(fn, payload)
        await asyncio.to_thread(_finish, job.id, result=result)
    except Retry as r:
        await asyncio.to_thread(_finish, job.id, error=str(r), retry_delay=r.delay_s)
    except Permanent as p:
        await asyncio.to_thread(_finish, job.id, error=str(p), permanent=True)
    except Exception:
        err = traceback.format_exc()
        log.warning("job %s (%s) failed: %s", job.id, job.kind, err.splitlines()[-1])
        await asyncio.to_thread(_finish, job.id, error=err)


@dataclass
class WorkerStats:
    processed: int = 0


def _pending_counts(kinds: list[str] | None) -> tuple[int, int]:
    """(ready-or-running jobs, jobs scheduled for later)."""
    with db.session() as s:
        base = select(func.count()).select_from(Job)
        if kinds:
            base = base.where(Job.kind.in_(kinds))
        now = utcnow()
        active = s.scalar(base.where(
            (Job.status == "running") | ((Job.status == "queued") & (Job.run_after <= now))))
        later = s.scalar(base.where(Job.status == "queued", Job.run_after > now))
        return active or 0, later or 0


async def run_worker(concurrency: int = 16, kinds: list[str] | None = None,
                     drain: bool = False, poll_s: float = 2.0, wait_scheduled: bool = False) -> WorkerStats:
    """Process jobs until stopped. With drain=True, return once nothing is
    ready or running (and, with wait_scheduled, nothing is scheduled later)."""
    from . import handlers  # noqa: F401  (registers handlers)

    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    requeue_stale()
    stats = WorkerStats()
    idle = asyncio.Event()

    async def loop(i: int) -> None:
        while True:
            job = await asyncio.to_thread(_claim, f"{worker_id}:{i}", kinds)
            if job is None:
                if drain:
                    active, later = await asyncio.to_thread(_pending_counts, kinds)
                    if active == 0 and (later == 0 or not wait_scheduled):
                        idle.set()
                        return
                await asyncio.sleep(poll_s)
                continue
            await _run_one(job)
            stats.processed += 1

    await asyncio.gather(*(loop(i) for i in range(concurrency)))
    return stats


def drain(kinds: list[str] | None = None, concurrency: int = 4) -> WorkerStats:
    """Synchronously run every ready job (used by tests and small runs)."""
    return asyncio.run(run_worker(concurrency=concurrency, kinds=kinds, drain=True, poll_s=0.05))


def job_counts() -> list[tuple[str, str, int]]:
    with db.session() as s:
        rows = s.execute(select(Job.kind, Job.status, func.count()).group_by(Job.kind, Job.status)).all()
        return [(k, st, n) for k, st, n in rows]

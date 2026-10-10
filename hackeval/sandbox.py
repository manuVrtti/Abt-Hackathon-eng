"""R4 deep review: build each finalist's frozen code in Docker and compare it
with the live link.

The container runs on an --internal Docker network (no internet), with CPU,
memory and process limits and no Linux capabilities. The hidden test suite is
run against the container and compared with the team's best live attempt: a
live link that passes tests the frozen code cannot pass suggests the
deployment is not the submitted code. Every outcome is a flag for a human
spot-check, never an automatic elimination.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

import httpx
from sqlalchemy import select

from . import db, jobs
from .config import get_settings
from .livetest.runner import run_tests, store_result
from .models import SandboxOverride, SandboxRun, Submission, TestAttempt, TestResult
from .review import raise_flag
from .snapshot import code_path
from .util import run

NODE_DOCKERFILE = """FROM node:20-slim
WORKDIR /app
COPY . .
RUN (npm ci || npm install) && (npm run build --if-present || true)
ENV PORT={port} HOST=0.0.0.0 NODE_ENV=production
EXPOSE {port}
CMD ["npm", "start"]
"""

PYTHON_DOCKERFILE = """FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt {extra}
ENV PORT={port}
EXPOSE {port}
CMD {cmd}
"""


def detect(src: Path, port: int) -> tuple[str, str | None, Path]:
    """Return (strategy, dockerfile text or None if the repo has its own, build context)."""
    if (src / "Dockerfile").exists():
        return "repo_dockerfile", None, src
    for sub in ("backend", "server", "api", "app"):
        if (src / sub / "Dockerfile").exists():
            return f"repo_dockerfile:{sub}", None, src / sub
    for ctx in (src, src / "backend", src / "server", src / "api"):
        pkg = ctx / "package.json"
        if pkg.exists():
            try:
                scripts = json.loads(pkg.read_text()).get("scripts", {})
            except ValueError:
                scripts = {}
            if "start" in scripts:
                return "generated:node", NODE_DOCKERFILE.format(port=port), ctx
        req = ctx / "requirements.txt"
        if req.exists():
            reqs = req.read_text(errors="ignore").lower()
            if (ctx / "manage.py").exists():
                cmd = f'["python", "manage.py", "runserver", "0.0.0.0:{port}"]'
                return "generated:django", PYTHON_DOCKERFILE.format(port=port, extra="", cmd=cmd), ctx
            for entry in ("main", "app", "server", "api"):
                if (ctx / f"{entry}.py").exists():
                    text = (ctx / f"{entry}.py").read_text(errors="ignore")
                    if "FastAPI(" in text or "fastapi" in reqs:
                        cmd = f'["uvicorn", "{entry}:app", "--host", "0.0.0.0", "--port", "{port}"]'
                        return "generated:fastapi", PYTHON_DOCKERFILE.format(port=port, extra="uvicorn", cmd=cmd), ctx
                    if "Flask(" in text or "flask" in reqs:
                        cmd = f'["gunicorn", "-b", "0.0.0.0:{port}", "{entry}:app"]'
                        return "generated:flask", PYTHON_DOCKERFILE.format(port=port, extra="gunicorn", cmd=cmd), ctx
    return "manual", None, src


def _docker(*args: str, timeout: float = 300):
    return run(["docker", *args], timeout=timeout)


def ensure_network(name: str) -> None:
    if _docker("network", "inspect", name, timeout=30).code != 0:
        _docker("network", "create", "--internal", name, timeout=60)


def compare(live: dict[str, bool], sandbox: dict[str, bool]) -> dict:
    only_live = sorted(t for t, ok in live.items() if ok and not sandbox.get(t, False))
    only_sandbox = sorted(t for t, ok in sandbox.items() if ok and not live.get(t, False))
    return {"pass_live_fail_sandbox": only_live, "pass_sandbox_fail_live": only_sandbox}


def _best_live(s, sub_id: int) -> tuple[float | None, dict[str, bool]]:
    best = s.scalars(select(TestAttempt).where(TestAttempt.submission_id == sub_id, TestAttempt.target == "live",
                                               TestAttempt.score.is_not(None))
                     .order_by(TestAttempt.score.desc())).first()
    if best is None:
        return None, {}
    res = s.scalars(select(TestResult).where(TestResult.submission_id == sub_id, TestResult.target == "live",
                                             TestResult.attempt == best.attempt)).all()
    return best.score, {r.test_id: r.passed for r in res}


@jobs.handler("sandbox")
async def sandbox_job(p: dict) -> dict:
    cfg = get_settings()
    sb = cfg.sandbox
    sub_id = p["submission_id"]

    def load():
        with db.session() as s:
            sub = s.get(Submission, sub_id)
            ov = s.get(SandboxOverride, sub_id)
            root = code_path(s, sub_id)
            live_score, live = _best_live(s, sub_id)
            return (sub.track, root, (ov.dockerfile, ov.subdir, ov.port, dict(ov.env or {})) if ov else None,
                    live_score, live)

    track, root, ov, live_score, live = await asyncio.to_thread(load)
    if root is None:
        raise jobs.Retry(300, "full code not fetched yet")
    if shutil.which("docker") is None:
        raise jobs.Permanent("docker is not installed on this server")

    work = cfg.path("sandbox", str(sub_id))
    src = work / "src"
    shutil.rmtree(src, ignore_errors=True)
    shutil.copytree(root, src, symlinks=True)
    port = (ov[2] if ov and ov[2] else None) or sb.default_port
    env = ov[3] if ov else {}
    if ov and ov[1]:
        src = src / ov[1]
    if ov and ov[0]:
        strategy, dockerfile, ctx = "override", ov[0], src
    else:
        strategy, dockerfile, ctx = detect(src, port)
    log_path = work / "sandbox.log"
    detail: dict = {"strategy": strategy, "port": port}
    tag, name = f"hackeval-sbx-{sub_id}", f"hackeval-sbx-{sub_id}"
    logs: list[str] = []

    def finish(verdict: str, build_ok=None, run_ok=None, sandbox_score=None, extra=None):
        log_path.write_text("\n\n".join(logs)[-2_000_000:])
        with db.session() as s:
            r = s.scalars(select(SandboxRun).where(SandboxRun.submission_id == sub_id)).first() or \
                SandboxRun(submission_id=sub_id)
            r.strategy, r.build_ok, r.run_ok, r.verdict = strategy, build_ok, run_ok, verdict
            r.sandbox_score, r.live_score, r.log_path = sandbox_score, live_score, str(log_path)
            r.detail = {**detail, **(extra or {})}
            s.merge(r)
            if verdict != "consistent":
                msg = {"manual": "The repository could not be built automatically; add a sandbox override.",
                       "build_failed": "The frozen code failed to build in the sandbox.",
                       "run_failed": "The frozen code built but did not start or answer in the sandbox.",
                       "mismatch": "The live link passes hidden tests that the frozen code fails in the sandbox."}
                raise_flag(s, sub_id, 4, f"sandbox_{verdict}", msg.get(verdict, verdict),
                           {**detail, **(extra or {}), "live_score": live_score, "sandbox_score": sandbox_score})
        return {"verdict": verdict, "sandbox_score": sandbox_score, "live_score": live_score}

    if strategy == "manual":
        return await asyncio.to_thread(finish, "manual")
    if dockerfile:
        (ctx / "Dockerfile.hackeval").write_text(dockerfile)
    dfile = str(ctx / ("Dockerfile.hackeval" if dockerfile else "Dockerfile"))
    build = ["build", "-t", tag, "-f", dfile]
    if not sb.build_network:
        build += ["--network", "none"]
    b = await asyncio.to_thread(_docker, *build, str(ctx), timeout=sb.build_timeout_s)
    logs.append("$ docker " + " ".join(build) + "\n" + b.out[-200000:] + b.err[-200000:])
    if b.code != 0:
        return await asyncio.to_thread(finish, "build_failed", build_ok=False)

    await asyncio.to_thread(ensure_network, sb.run_network)
    await asyncio.to_thread(_docker, "rm", "-f", name, timeout=60)
    run_args = ["run", "-d", "--name", name, "--network", sb.run_network, "--cpus", str(sb.cpus),
                "--memory", sb.memory, "--pids-limit", str(sb.pids_limit), "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "-e", f"PORT={port}"]
    for k, v in env.items():
        run_args += ["-e", f"{k}={v}"]
    r = await asyncio.to_thread(_docker, *run_args, tag, timeout=120)
    logs.append("$ docker run ...\n" + r.out + r.err)
    try:
        if r.code != 0:
            return await asyncio.to_thread(finish, "run_failed", build_ok=True, run_ok=False)
        ip = (await asyncio.to_thread(_docker, "inspect", "-f",
                                      "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name)).out.strip()
        base = f"http://{ip}:{port}"
        detail["base_url"] = base
        deadline = time.monotonic() + sb.start_timeout_s
        up = False
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                try:
                    await client.get(base + "/", timeout=5)
                    up = True
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(2)
        if not up:
            lg = await asyncio.to_thread(_docker, "logs", "--tail", "500", name)
            logs.append("$ docker logs\n" + lg.out + lg.err)
            return await asyncio.to_thread(finish, "run_failed", build_ok=True, run_ok=False)
        res = await run_tests(sub_id, base, track)

        def save():
            with db.session() as s:
                store_result(s, sub_id, 1, "sandbox", res)
        await asyncio.to_thread(save)
        sandbox_map = {o.test_id: o.passed for o in res.outcomes}
        cmp = compare(live, sandbox_map)
        gap = (live_score or 0) - res.score
        verdict = "mismatch" if gap > sb.integrity_gap_flag else "consistent"
        lg = await asyncio.to_thread(_docker, "logs", "--tail", "500", name)
        logs.append("$ docker logs\n" + lg.out + lg.err)
        return await asyncio.to_thread(finish, verdict, True, True, res.score, {**cmp, "gap": round(gap, 2)})
    finally:
        await asyncio.to_thread(_docker, "rm", "-f", name, timeout=60)
        await asyncio.to_thread(_docker, "rmi", "-f", tag, timeout=120)


def start_r4() -> int:
    from .models import ACTIVE

    with db.session() as s:
        subs = s.scalars(select(Submission).where(Submission.status == ACTIVE, Submission.round >= 4)).all()
        for sub in subs:
            jobs.enqueue(s, "fetch_full", str(sub.id), {"submission_id": sub.id}, max_attempts=4)
            jobs.enqueue(s, "sandbox", str(sub.id), {"submission_id": sub.id}, max_attempts=2)
    return len(subs)


def advance_r4(max_teams: int = 50, order_by: str = "total") -> dict:
    """Teams with a consistent sandbox result (or a human-cleared flag) and no
    open R4 flags go to judging, best first, up to max_teams."""
    from .models import ACTIVE, ELIMINATED, Flag, Score

    with db.session() as s:
        subs = s.scalars(select(Submission).where(Submission.status == ACTIVE, Submission.round == 4)).all()
        eligible, blocked = [], []
        for sub in subs:
            open_r4 = s.scalars(select(Flag).where(Flag.submission_id == sub.id, Flag.round == 4,
                                                   Flag.status == "open")).first()
            run_ = s.scalars(select(SandboxRun).where(SandboxRun.submission_id == sub.id)).first()
            if open_r4 or run_ is None:
                blocked.append(sub.team_id)
                continue
            sc = s.scalars(select(Score).where(Score.submission_id == sub.id, Score.component == order_by)).first()
            eligible.append((sc.value if sc else 0.0, sub))
        eligible.sort(key=lambda x: -x[0])
        if blocked:
            return {"blocked": blocked, "message": "Resolve open R4 flags / missing sandbox runs first."}
        keep = {sub.id for _, sub in eligible[:max_teams]}
        for _, sub in eligible:
            if sub.id in keep:
                sub.round = 5
            else:
                sub.status, sub.eliminated_round = ELIMINATED, 4
                sub.status_reason = "Not among the teams selected for judging after deep review."
        db.log_event(s, "operator", "r4_advanced", None, kept=len(keep))
    return {"advanced": len(keep), "eliminated": len(eligible) - len(keep)}

"""R0 freeze.

1. ``freeze_heads`` runs at the deadline: one GitHub GraphQL call per 100
   repos records each default-branch head SHA plus server-side facts
   (createdAt, fork, visibility, size). ``git ls-remote`` covers anything the
   API missed. This takes minutes, so the frozen commit is the one that
   existed at the deadline even though cloning 10,000 repos takes hours.
2. ``snapshot`` jobs make blob-less bare clones (history and trees only).
3. ``fetch_full`` jobs fetch complete code for R1 survivors, write a git
   bundle of the frozen commit and extract its files for analysis.
4. ``recheck`` jobs run 24 hours later and detect rewritten history.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tarfile
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from . import db, github_api, jobs
from .config import get_settings
from .gitstats import GIT_ENV, GitError, git
from .models import ACTIVE, Flag, RepoMeta, Snapshot, Submission
from .util import run, sha256_file, utcnow

log = logging.getLogger("hackeval.snapshot")


def clone_url(sub: Submission) -> str:
    if sub.repo_owner and sub.repo_name:
        return f"https://github.com/{sub.repo_owner}/{sub.repo_name}.git"
    # Local paths / file:// URLs are accepted for dry runs and tests.
    return sub.repo_url


def repo_dir(sub_id: int) -> Path:
    return get_settings().path("repos") / f"{sub_id}.git"


def extract_tree(repo: Path, sha: str, dest: Path, env: dict) -> str | None:
    """Write the commit's files to dest via `git archive` (portable: no shell or tar binary).
    Returns an error message, or None on success."""
    archive = dest.parent / f"{dest.name}.tar"
    try:
        r = run(["git", "-C", str(repo), "archive", "--format=tar", f"--output={archive}", sha],
                timeout=900, env=env)
        if r.code != 0:
            return r.err
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")  # "data" blocks absolute paths, .. and device files
        return None
    except (OSError, tarfile.TarError) as e:
        return str(e)
    finally:
        archive.unlink(missing_ok=True)


def _git_cmd(args: list[str], timeout: float, cwd: Path | None = None):
    return run(["git", *args], cwd=cwd, timeout=timeout, env=GIT_ENV)


# ---------------------------------------------------------------- freeze heads

def _ls_remote_head(url: str, timeout: float = 60) -> tuple[str | None, str | None, str | None]:
    r = run(["git", "ls-remote", "--symref", url, "HEAD"], timeout=timeout, env=GIT_ENV)
    if r.code != 0:
        return None, None, r.err.strip()[:500]
    branch = sha = None
    for line in r.out.splitlines():
        if line.startswith("ref:"):
            branch = line.split()[1].removeprefix("refs/heads/")
        elif line.endswith("\tHEAD"):
            sha = line.split()[0]
    return sha, branch, None if sha else "empty repository"


def freeze_heads(transport=None) -> dict:
    """Record the head commit of every active submission. Safe to re-run:
    already-frozen submissions are left untouched."""
    cfg = get_settings()
    with db.session() as s:
        subs = s.scalars(select(Submission).where(Submission.status == ACTIVE,
                                                  Submission.frozen_sha.is_(None))).all()
        gh = [(x.id, x.repo_owner, x.repo_name) for x in subs if x.repo_owner]
        others = [(x.id, clone_url(x)) for x in subs if not x.repo_owner]

    facts_by_id: dict[int, github_api.RepoFacts] = {}
    token = cfg.github_token
    if gh and token:
        facts = asyncio.run(github_api.fetch_all(token, [(o, n) for _, o, n in gh], transport=transport))
        facts_by_id = {sid: f for (sid, _, _), f in zip(gh, facts)}
    elif gh:
        log.warning("No GitHub token (%s); repo metadata rules will be flagged for review", cfg.github_token_env)

    now = utcnow()
    stats = {"api": 0, "ls_remote": 0, "unresolved": 0}
    need_ls = []
    with db.session() as s:
        replay = cfg.event.replay
        for sid, f in facts_by_id.items():
            meta = s.get(RepoMeta, sid) or RepoMeta(submission_id=sid)
            meta.exists = f.exists
            meta.is_private = f.is_private
            meta.is_fork = f.is_fork
            meta.parent = f.parent
            meta.created_at = f.created_at
            meta.pushed_at = f.pushed_at
            meta.disk_usage_kb = f.disk_usage_kb
            meta.default_branch = f.default_branch
            meta.head_sha = f.head_sha
            meta.raw = f.raw
            meta.error = f.error
            meta.fetched_at = now
            s.merge(meta)
            sub = s.get(Submission, sid)
            if replay:
                continue  # resolved from history by the snapshot job
            if f.head_sha:
                sub.frozen_sha, sub.frozen_branch = f.head_sha, f.default_branch
                sub.frozen_at, sub.frozen_source = now, "github_api"
                stats["api"] += 1
            else:
                need_ls.append((sid, clone_url(sub)))
        for sid, _o, _n in gh:
            if sid not in facts_by_id:
                need_ls.append((sid, clone_url(s.get(Submission, sid))))
    need_ls += others
    if cfg.event.replay:
        need_ls = []

    def ls(item):
        sid, url = item
        return sid, _ls_remote_head(url)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=cfg.snapshot.concurrency) as pool:
        results = list(pool.map(ls, need_ls))
    with db.session() as s:
        for sid, (sha, branch, _err) in results:
            sub = s.get(Submission, sid)
            if sha:
                sub.frozen_sha, sub.frozen_branch = sha, branch
                sub.frozen_at, sub.frozen_source = utcnow(), "ls_remote"
                stats["ls_remote"] += 1
            else:
                stats["unresolved"] += 1
        for sub in s.scalars(select(Submission).where(Submission.status == ACTIVE)):
            sub.round = max(sub.round, 1)
            jobs.enqueue(s, "snapshot", str(sub.id), {"submission_id": sub.id}, max_attempts=1)
    return stats


# ---------------------------------------------------------------- snapshot job

def _upsert_snapshot(s, sub_id: int, kind: str) -> Snapshot:
    snap = s.scalars(select(Snapshot).where(Snapshot.submission_id == sub_id, Snapshot.kind == kind)).first()
    if snap is None:
        snap = Snapshot(submission_id=sub_id, kind=kind)
        s.add(snap)
    return snap


@jobs.handler("snapshot")
def snapshot_job(p: dict) -> dict:
    cfg = get_settings()
    sub_id = p["submission_id"]
    attempt = p.get("_attempt", 1)
    with db.session() as s:
        sub = s.get(Submission, sub_id)
        url, frozen = clone_url(sub), sub.frozen_sha
    dest = repo_dir(sub_id)
    t0 = time.monotonic()
    error = None
    if not (dest / "HEAD").exists():
        tmp = dest.with_suffix(".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        r = _git_cmd(["clone", "--bare", "--filter=blob:none", "--no-tags", url, str(tmp)],
                     timeout=cfg.snapshot.clone_timeout_s)
        if r.code == 0:
            tmp.rename(dest)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
            error = r.err.strip()[-1000:]

    if error is None:
        try:
            if frozen:
                try:
                    git(dest, "cat-file", "-e", f"{frozen}^{{commit}}")
                except GitError:
                    # Pushed and then rewritten between freeze and clone: fetch the frozen SHA.
                    _git_cmd(["-C", str(dest), "fetch", "--filter=blob:none", "origin", frozen], timeout=300)
                    git(dest, "cat-file", "-e", f"{frozen}^{{commit}}")
                sha = frozen
            elif cfg.event.replay:
                before = cfg.event.deadline.isoformat()
                sha = git(dest, "rev-list", "-1", "--first-parent", f"--before={before}", "HEAD").strip()
                if not sha:
                    raise GitError(f"no commit before the deadline {before}")
            else:
                sha = git(dest, "rev-parse", "HEAD").strip()
            tree = git(dest, "rev-parse", f"{sha}^{{tree}}").strip()
        except GitError as e:
            error = str(e)

    if error is not None:
        delays = cfg.snapshot.retry_delays_min
        if attempt < cfg.snapshot.clone_attempts:
            raise jobs.Retry(60 * delays[min(attempt - 1, len(delays) - 1)], f"clone failed: {error[:300]}")
        with db.session() as s:
            snap = _upsert_snapshot(s, sub_id, "freeze")
            snap.ok, snap.error, snap.attempts = False, error, attempt
            snap.duration_s = time.monotonic() - t0
            jobs.enqueue(s, "r1_rules", str(sub_id), {"submission_id": sub_id})
        return {"ok": False, "error": error[:300]}

    size_kb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) // 1024
    with db.session() as s:
        sub = s.get(Submission, sub_id)
        if not sub.frozen_sha:
            sub.frozen_sha, sub.frozen_at = sha, utcnow()
            sub.frozen_source = "replay_history" if cfg.event.replay else "clone"
        snap = _upsert_snapshot(s, sub_id, "freeze")
        snap.ok, snap.error, snap.attempts = True, None, attempt
        snap.commit_sha, snap.tree_sha = sha, tree
        snap.clone_path, snap.size_kb = str(dest), size_kb
        snap.duration_s = time.monotonic() - t0
        jobs.enqueue(s, "r1_rules", str(sub_id), {"submission_id": sub_id})
        if cfg.snapshot.recheck_after_hours > 0 and not cfg.event.replay:
            jobs.enqueue(s, "recheck", str(sub_id), {"submission_id": sub_id},
                         run_after=utcnow() + timedelta(hours=cfg.snapshot.recheck_after_hours))
    return {"ok": True, "sha": sha}


# ---------------------------------------------------------------- recheck job

@jobs.handler("recheck")
def recheck_job(p: dict) -> dict:
    """Second look 24 hours later: was the frozen history rewritten or the repo removed?"""
    sub_id = p["submission_id"]
    with db.session() as s:
        sub = s.get(Submission, sub_id)
        snap = s.scalars(select(Snapshot).where(Snapshot.submission_id == sub_id,
                                                Snapshot.kind == "freeze", Snapshot.ok.is_(True))).first()
        if snap is None:
            return {"skipped": "no freeze snapshot"}
        frozen, branch, url = snap.commit_sha, sub.frozen_branch, clone_url(sub)
    dest = Path(snap.clone_path)
    new_head, ln_branch, err = _ls_remote_head(url)
    result: dict = {"frozen": frozen, "remote_head": new_head, "branch": ln_branch}
    flag = None
    if new_head is None:
        result["status"] = "unavailable"
        flag = ("repo_unavailable_after_deadline",
                "The repository could not be read 24 hours after the deadline.", {"error": err})
    elif new_head == frozen:
        result["status"] = "unchanged"
    else:
        r = _git_cmd(["-C", str(dest), "fetch", "--filter=blob:none", "--no-tags", "origin",
                      "+HEAD:refs/hackeval/recheck"], timeout=600)
        if r.code != 0:
            result["status"] = "fetch_failed"
            result["error"] = r.err[-500:]
        else:
            anc = _git_cmd(["-C", str(dest), "merge-base", "--is-ancestor", frozen, "refs/hackeval/recheck"],
                           timeout=120)
            if anc.code == 0:
                result["status"] = "fast_forward"  # new commits after the deadline: ignored
            else:
                result["status"] = "rewritten"
                flag = ("history_rewritten",
                        "The frozen deadline commit is no longer in the branch history 24 hours later "
                        "(force-push or history rewrite). Only the frozen version is judged.",
                        {"frozen_sha": frozen, "head_24h_later": new_head, "branch": branch})
    with db.session() as s:
        snap2 = _upsert_snapshot(s, sub_id, "recheck")
        snap2.ok = result["status"] in ("unchanged", "fast_forward")
        snap2.commit_sha = new_head
        snap2.detail = result
        if flag:
            from .review import raise_flag

            raise_flag(s, sub_id, 1, flag[0], flag[1], {**flag[2], **result})
    return result


# ---------------------------------------------------------------- full code

@jobs.handler("fetch_full")
def fetch_full_job(p: dict) -> dict:
    """Fetch complete code for a survivor, bundle the frozen commit, extract files."""
    cfg = get_settings()
    sub_id = p["submission_id"]
    with db.session() as s:
        sub = s.get(Submission, sub_id)
        snap = s.scalars(select(Snapshot).where(Snapshot.submission_id == sub_id,
                                                Snapshot.kind == "freeze", Snapshot.ok.is_(True))).first()
        if snap is None:
            raise jobs.Permanent("no freeze snapshot")
        sha, url, partial = snap.commit_sha, clone_url(sub), Path(snap.clone_path)
    t0 = time.monotonic()
    tmp = cfg.path("tmp") / f"full-{sub_id}.git"
    shutil.rmtree(tmp, ignore_errors=True)
    work, env, source = tmp, GIT_ENV, "full_clone"

    def has_sha() -> bool:
        return run(["git", "-C", str(work), "cat-file", "-e", f"{sha}^{{commit}}"], timeout=60, env=env).code == 0

    r = _git_cmd(["clone", "--mirror", url, str(tmp)], timeout=cfg.snapshot.clone_timeout_s)
    if r.code == 0 and not has_sha():
        _git_cmd(["-C", str(tmp), "fetch", "origin", sha], timeout=600)
    if r.code != 0 or not has_sha():
        # Last resort: the deadline partial clone, letting git fetch missing blobs lazily.
        shutil.rmtree(tmp, ignore_errors=True)
        work, source = partial, "partial_lazy"
        env = {k: v for k, v in GIT_ENV.items() if k != "GIT_NO_LAZY_FETCH"}
    try:
        run(["git", "-C", str(work), "update-ref", "refs/tags/hackeval-frozen", sha], timeout=60, env=env)
        bundle = cfg.path("bundles") / f"{sub_id}.bundle"
        rb = run(["git", "-C", str(work), "bundle", "create", str(bundle), "refs/tags/hackeval-frozen"],
                 timeout=1800, env=env)
        if rb.code != 0:
            raise jobs.Retry(600, f"bundle failed ({source}): {rb.err[-300:]} {r.err[-200:]}")
        code = cfg.path("code") / str(sub_id)
        shutil.rmtree(code, ignore_errors=True)
        code.mkdir(parents=True)
        err = extract_tree(work, sha, code, env)
        if err:
            raise jobs.Retry(600, f"extract failed: {err[-300:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with db.session() as s:
        full = _upsert_snapshot(s, sub_id, "full")
        full.ok, full.commit_sha = True, sha
        full.bundle_path, full.code_path = str(bundle), str(code)
        full.bundle_sha256 = sha256_file(bundle)
        full.size_kb = bundle.stat().st_size // 1024
        full.duration_s = time.monotonic() - t0
        full.detail = {"source": source}
        # Code-level R2 checks start as soon as the code is on disk.
        jobs.enqueue(s, "quality", str(sub_id), {"submission_id": sub_id})
        jobs.enqueue(s, "fingerprint", str(sub_id), {"submission_id": sub_id})
    return {"ok": True, "source": source}


def code_path(s, sub_id: int) -> Path | None:
    snap = s.scalars(select(Snapshot).where(Snapshot.submission_id == sub_id, Snapshot.kind == "full",
                                            Snapshot.ok.is_(True))).first()
    return Path(snap.code_path) if snap and snap.code_path else None


def open_flags(s, sub_id: int) -> list[Flag]:
    return s.scalars(select(Flag).where(Flag.submission_id == sub_id, Flag.status == "open")).all()

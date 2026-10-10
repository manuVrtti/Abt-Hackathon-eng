"""R1 rule gates.

Each rule returns pass, hard_fail or flag with stored proof. Hard fails use
only objective facts: GitHub's server records and our own deadline snapshot,
never dates written inside git (which participants can fake).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ENGINE_VERSION, db, jobs
from .config import get_settings
from .gitstats import GitError, HistoryStats, analyse
from .models import (
    ACTIVE, DISQUALIFIED, ERROR, FLAG, HARD_FAIL, PASS, SKIPPED, Check, RepoMeta, Snapshot, Submission,
)
from .review import disqualify, raise_flag
from .util import aware


@dataclass
class Outcome:
    rule: str
    result: str
    summary: str
    proof: dict = field(default_factory=dict)


def record(s: Session, sub_id: int, round_: int, o: Outcome) -> Check:
    c = s.scalars(select(Check).where(Check.submission_id == sub_id, Check.rule == o.rule)).first()
    if c is None:
        c = Check(submission_id=sub_id, rule=o.rule, round=round_)
        s.add(c)
    elif c.overturned and c.result == o.result:
        return c  # keep a reviewer's decision when re-running with the same outcome
    c.result, c.summary, c.proof, c.engine_version, c.round = o.result, o.summary, o.proof, ENGINE_VERSION, round_
    c.overturned = False
    s.flush()
    return c


def blobs_path(sub_id: int) -> Path:
    return get_settings().path("r1", "blobs") / f"{sub_id}.json"


def evaluate(sub: Submission, meta: RepoMeta | None, snap: Snapshot | None,
             stats: HistoryStats | None, stats_error: str | None) -> list[Outcome]:
    cfg = get_settings()
    rules, ev = cfg.rules, cfg.event
    out: list[Outcome] = []
    meta_proof = None
    if meta is not None:
        meta_proof = {"source": "GitHub GraphQL API", "fetched_at": aware(meta.fetched_at).isoformat(),
                      "exists": meta.exists, "is_private": meta.is_private, "is_fork": meta.is_fork,
                      "parent": meta.parent, "createdAt": meta.created_at.isoformat() if meta.created_at else None,
                      "api_error": meta.error}

    # 1. Repository is public and present.
    if not sub.repo_owner and not Path(sub.repo_url).exists() and not sub.repo_url.startswith("file://"):
        out.append(Outcome("repo_url_valid", HARD_FAIL, "The submitted repository URL is not a GitHub repository URL.",
                           {"submitted_repo_url": sub.repo_url}))
        return out
    clone_ok = bool(snap and snap.ok)
    if clone_ok:
        out.append(Outcome("repo_accessible", PASS, "Repository cloned at the deadline.",
                           {"commit": snap.commit_sha, "attempts": snap.attempts}))
    else:
        proof = {"clone_attempts": snap.attempts if snap else 0, "clone_error": (snap.error if snap else "not attempted"),
                 "github_api": meta_proof}
        api_confirms = meta is not None and (meta.exists is False or meta.is_private is True) \
            and not (meta.error or "").startswith("api_error")
        if api_confirms:
            out.append(Outcome("repo_accessible", HARD_FAIL,
                               "The repository is private or does not exist: cloning failed on every attempt and "
                               "GitHub's API confirms it is not publicly visible.", proof))
        else:
            out.append(Outcome("repo_accessible", FLAG,
                               "Cloning failed but GitHub's API did not confirm the repository is missing or private.",
                               proof))
        return out

    # 2. Repository created before the hackathon started (GitHub server time).
    if meta is not None and meta.created_at is not None:
        created = aware(meta.created_at)
        p = {**meta_proof, "hackathon_start": ev.start.isoformat()}
        if created < ev.start:
            out.append(Outcome("repo_created_after_start", HARD_FAIL,
                               f"GitHub records the repository as created at {created.isoformat()}, before the "
                               f"hackathon started at {ev.start.isoformat()}.", p))
        else:
            out.append(Outcome("repo_created_after_start", PASS, "Repository created after the start.", p))
    else:
        out.append(Outcome("repo_created_after_start", FLAG if sub.repo_owner else SKIPPED,
                           "Repository creation time could not be read from GitHub.", {"github_api": meta_proof}))

    if stats is None:
        out.append(Outcome("history_readable", FLAG, "Commit history could not be analysed.", {"error": stats_error}))
        return out
    hist = stats.summary()

    # 3. Minimum commit count in the frozen history.
    p = {"frozen_commit": stats.head, "commit_count": stats.commit_count, "minimum": rules.min_commits}
    if stats.commit_count < rules.min_commits:
        out.append(Outcome("min_commits", HARD_FAIL,
                           f"The frozen history has {stats.commit_count} commits; the published minimum is "
                           f"{rules.min_commits}.", p))
    else:
        out.append(Outcome("min_commits", PASS, f"{stats.commit_count} commits.", p))

    # 4. Old code dumped in by the first commit.
    p = {"frozen_commit": stats.head, "root_commits": stats.root_commits,
         "final_code_files": stats.final_code_files,
         "share_of_final_code_files_from_first_commit": stats.first_commit_code_share,
         "largest_single_commit_share": stats.largest_commit_share,
         "largest_commit": stats.largest_commit_sha, "threshold": rules.first_commit_share_flag,
         "method": "Share of final code files whose exact content (git blob) was introduced by the first commit."}
    if stats.final_code_files >= 3 and stats.first_commit_code_share > rules.first_commit_share_flag:
        out.append(Outcome("first_commit_dump", FLAG,
                           f"The first commit already contains {stats.first_commit_code_share:.0%} of the final code.", p))
    else:
        out.append(Outcome("first_commit_dump", PASS, "No bulk import in the first commit.", p))

    # 5. Commits authored before the start (imported history). Author dates can be faked,
    #    but honest dates before the start are worth a human look.
    if stats.commits_before_start > 0:
        out.append(Outcome("commits_before_start", FLAG,
                           f"{stats.commits_before_start} commits carry author dates before the hackathon started.",
                           {"commits_before_start": stats.commits_before_start, "history": hist,
                            "note": "Git dates are self-reported; this is a flag, never a hard fail."}))
    else:
        out.append(Outcome("commits_before_start", PASS, "No commits dated before the start.", {}))

    # 6. Freeze timing: anything pushed after the deadline is ignored, but if the
    #    head was recorded late the frozen commit may postdate the deadline.
    frozen_at = aware(sub.frozen_at)
    late = frozen_at is not None and frozen_at > ev.deadline + timedelta(minutes=10)
    if late and stats.commits_after_deadline > 0:
        out.append(Outcome("post_deadline_commits", FLAG,
                           "The head was recorded after the deadline and the frozen history contains commits "
                           "dated after the deadline.",
                           {"frozen_at": frozen_at.isoformat(), "deadline": ev.deadline.isoformat(),
                            "commits_after_deadline": stats.commits_after_deadline}))
    else:
        out.append(Outcome("post_deadline_commits", PASS, "Only the deadline version is judged.",
                           {"frozen_at": frozen_at.isoformat() if frozen_at else None,
                            "frozen_source": sub.frozen_source}))

    # 7. Fork of another repository.
    if meta is not None and meta.is_fork:
        out.append(Outcome("fork", FLAG, f"GitHub marks the repository as a fork of {meta.parent}.", meta_proof))
    elif meta is not None and meta.is_fork is False:
        out.append(Outcome("fork", PASS, "Not a fork.", {}))
    else:
        out.append(Outcome("fork", SKIPPED, "Fork status could not be read from GitHub.", {"github_api": meta_proof}))

    # 8. Published size limit.
    size_kb = meta.disk_usage_kb if meta and meta.disk_usage_kb is not None else snap.size_kb
    limit_kb = rules.repo_size_limit_mb * 1024
    if size_kb is not None and size_kb > limit_kb:
        out.append(Outcome("repo_size", HARD_FAIL if rules.repo_size_action == "hard_fail" else FLAG,
                           f"The repository is {size_kb / 1024:.0f} MB; the published limit is "
                           f"{rules.repo_size_limit_mb} MB.", {"size_kb": size_kb, "limit_kb": limit_kb}))
    else:
        out.append(Outcome("repo_size", PASS, "Within the size limit.", {"size_kb": size_kb}))
    return out


@jobs.handler("r1_rules")
def r1_rules_job(p: dict) -> dict:
    cfg = get_settings()
    sub_id = p["submission_id"]
    with db.session() as s:
        sub = s.get(Submission, sub_id)
        meta = s.get(RepoMeta, sub_id)
        snap = s.scalars(select(Snapshot).where(Snapshot.submission_id == sub_id, Snapshot.kind == "freeze")).first()
        s.expunge_all()
    stats = err = None
    if snap and snap.ok:
        try:
            stats = analyse(Path(snap.clone_path), snap.commit_sha, cfg.rules, cfg.event.start, cfg.event.deadline)
            blobs_path(sub_id).write_text(json.dumps(stats.code_blobs))
        except GitError as e:
            err = str(e)
    outcomes = evaluate(sub, meta, snap, stats, err)
    with db.session() as s:
        for o in outcomes:
            record(s, sub_id, 1, o)
        if stats:
            snap2 = s.get(Snapshot, snap.id)
            snap2.detail = {**(snap2.detail or {}), "history": stats.summary()}
    return {o.rule: o.result for o in outcomes}


def apply_r1_decisions() -> dict:
    """Turn checks into outcomes: hard fail -> provisional disqualification with
    evidence and appeal link; flags -> human queue; otherwise advance to R2."""
    counts = Counter()
    with db.session() as s:
        subs = s.scalars(select(Submission).where(Submission.round >= 1)).all()
        for sub in subs:
            checks = s.scalars(select(Check).where(Check.submission_id == sub.id, Check.round == 1)).all()
            if not checks:
                counts["pending"] += 1
                continue
            hard = [c for c in checks if c.result == HARD_FAIL and not c.overturned]
            for c in checks:
                if c.result in (FLAG, ERROR):
                    raise_flag(s, sub.id, 1, f"rule:{c.rule}", c.summary, c.proof)
            if hard:
                if sub.status == ACTIVE:
                    disqualify(s, sub, 1, "; ".join(c.summary for c in hard), checks=hard)
                counts["hard_fail"] += 1
            elif sub.status == ACTIVE:
                sub.round = max(sub.round, 2)
                counts["advanced"] += 1
            elif sub.status == DISQUALIFIED:
                counts["disqualified_other"] += 1
    return dict(counts)


# ------------------------------------------------------------ cross-team rules

def cross_team_checks() -> dict:
    """Same repo, same live link, identical code or largely shared code across teams -> flags."""
    from datasketch import MinHash, MinHashLSH

    cfg = get_settings()
    found = Counter()
    with db.session() as s:
        subs = s.scalars(select(Submission).where(Submission.round >= 1)).all()
        snaps = {sn.submission_id: sn for sn in s.scalars(select(Snapshot).where(Snapshot.kind == "freeze",
                                                                                 Snapshot.ok.is_(True)))}
        metas = {m.submission_id: m for m in s.scalars(select(RepoMeta))}

        def flag_group(ids: list[int], type_: str, reason: str, evidence: dict):
            for a in ids:
                for b in ids:
                    if a != b:
                        raise_flag(s, a, 1, type_, reason, evidence, related_id=b)
                        found[type_] += 1

        by_repo, by_live, by_tree = defaultdict(list), defaultdict(list), defaultdict(list)
        repo_to_sub = {}
        for sub in subs:
            key = f"{sub.repo_owner}/{sub.repo_name}".lower() if sub.repo_owner else sub.repo_url
            by_repo[key].append(sub.id)
            repo_to_sub[key] = sub.id
            if sub.live_url:
                by_live[sub.live_url.lower().rstrip("/")].append(sub.id)
            if sub.id in snaps and snaps[sub.id].tree_sha:
                by_tree[snaps[sub.id].tree_sha].append(sub.id)
        for k, ids in by_repo.items():
            if len(ids) > 1:
                flag_group(ids, "duplicate_repo", f"The same repository ({k}) was submitted by several teams.",
                           {"repo": k, "submission_ids": ids})
        for k, ids in by_live.items():
            if len(ids) > 1:
                flag_group(ids, "duplicate_live_url", f"The same live link ({k}) was submitted by several teams.",
                           {"live_url": k, "submission_ids": ids})
        for k, ids in by_tree.items():
            if len(ids) > 1:
                flag_group(ids, "identical_code", "Several teams' frozen commits have byte-identical file trees.",
                           {"tree_sha": k, "submission_ids": ids})
        # A fork of another team's repository.
        for sub in subs:
            m = metas.get(sub.id)
            if m and m.parent and m.parent.lower() in repo_to_sub and repo_to_sub[m.parent.lower()] != sub.id:
                other = repo_to_sub[m.parent.lower()]
                raise_flag(s, sub.id, 1, "fork_of_other_team", f"Forked from another team's repository {m.parent}.",
                           {"parent": m.parent}, related_id=other)
                found["fork_of_other_team"] += 1

        # Shared code: Jaccard similarity of code-file blob sets, boilerplate removed.
        blob_sets: dict[int, set[str]] = {}
        for sub in subs:
            p = blobs_path(sub.id)
            if p.exists():
                blob_sets[sub.id] = set(json.loads(p.read_text()))
        df = Counter(b for bs in blob_sets.values() for b in bs)
        common = {b for b, n in df.items() if n >= cfg.rules.boilerplate_min_repos}
        lsh = MinHashLSH(threshold=max(0.3, cfg.rules.duplicate_jaccard_flag - 0.2), num_perm=128)
        hashes = {}
        for sid, bs in blob_sets.items():
            bs = bs - common
            blob_sets[sid] = bs
            if len(bs) < 3:
                continue
            m = MinHash(num_perm=128)
            for b in bs:
                m.update(b.encode())
            hashes[sid] = m
            lsh.insert(str(sid), m)
        seen = set()
        for sid, m in hashes.items():
            for other in lsh.query(m):
                o = int(other)
                if o == sid or (min(sid, o), max(sid, o)) in seen:
                    continue
                seen.add((min(sid, o), max(sid, o)))
                a, b = blob_sets[sid], blob_sets[o]
                j = len(a & b) / len(a | b)
                if snaps[sid].tree_sha == snaps[o].tree_sha:
                    continue  # already flagged as identical_code
                if j >= cfg.rules.duplicate_jaccard_flag:
                    ev = {"jaccard": round(j, 3), "shared_files": len(a & b), "files_a": len(a), "files_b": len(b),
                          "method": "Identical code-file contents (git blob IDs), boilerplate shared by "
                                    f">= {cfg.rules.boilerplate_min_repos} repos removed."}
                    flag_group([sid, o], "shared_code", f"{j:.0%} of code files are byte-identical with another team.",
                               ev)
    return dict(found)

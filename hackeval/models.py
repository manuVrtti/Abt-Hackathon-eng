"""Database tables. Runs on PostgreSQL in production and SQLite in tests."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .util import utcnow


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON, datetime: DateTime(timezone=True)}


# Submission.status values
ACTIVE = "active"
DISQUALIFIED = "disqualified"
ELIMINATED = "eliminated"  # cut by ranking, voting or judging (not a rule break)

# Check.result values
PASS = "pass"
HARD_FAIL = "hard_fail"
FLAG = "flag"
ERROR = "error"
SKIPPED = "skipped"


class Submission(Base):
    __tablename__ = "submissions"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[str] = mapped_column(String(128), unique=True)
    team_name: Mapped[str] = mapped_column(String(256), default="")
    email: Mapped[str] = mapped_column(String(256), default="")
    track: Mapped[str] = mapped_column(String(64), default="default")
    repo_url: Mapped[str] = mapped_column(String(512))
    repo_owner: Mapped[str] = mapped_column(String(128), default="")
    repo_name: Mapped[str] = mapped_column(String(128), default="")
    live_url: Mapped[str] = mapped_column(String(1024), default="")
    submitted_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(String(32), default=ACTIVE)
    # Furthest round the team has entered (0 = registered, 1 = frozen, 2 = passed R1 ...).
    round: Mapped[int] = mapped_column(Integer, default=0)
    eliminated_round: Mapped[int | None]
    status_reason: Mapped[str] = mapped_column(Text, default="")
    # Head commit recorded at the deadline (from GitHub's API or ls-remote).
    frozen_sha: Mapped[str | None] = mapped_column(String(64))
    frozen_branch: Mapped[str | None] = mapped_column(String(256))
    frozen_at: Mapped[datetime | None]
    frozen_source: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    snapshots: Mapped[list["Snapshot"]] = relationship(back_populates="submission")


class RepoMeta(Base):
    """GitHub server-side facts. Participants cannot edit these."""

    __tablename__ = "repo_meta"
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), primary_key=True)
    exists: Mapped[bool | None]
    is_private: Mapped[bool | None]
    is_fork: Mapped[bool | None]
    parent: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime | None]
    pushed_at: Mapped[datetime | None]
    disk_usage_kb: Mapped[int | None]
    default_branch: Mapped[str | None] = mapped_column(String(256))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    raw: Mapped[dict | None]
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)


class Snapshot(Base):
    __tablename__ = "snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    # freeze: partial clone at deadline; recheck: 24h later; full: complete code for survivors
    kind: Mapped[str] = mapped_column(String(16))
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    tree_sha: Mapped[str | None] = mapped_column(String(64))
    clone_path: Mapped[str | None] = mapped_column(String(1024))
    bundle_path: Mapped[str | None] = mapped_column(String(1024))
    code_path: Mapped[str | None] = mapped_column(String(1024))
    bundle_sha256: Mapped[str | None] = mapped_column(String(64))
    size_kb: Mapped[int | None]
    duration_s: Mapped[float | None]
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None]
    taken_at: Mapped[datetime] = mapped_column(default=utcnow)

    submission: Mapped[Submission] = relationship(back_populates="snapshots")
    __table_args__ = (UniqueConstraint("submission_id", "kind"),)


class Check(Base):
    __tablename__ = "checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    round: Mapped[int] = mapped_column(Integer)
    rule: Mapped[str] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(16))
    summary: Mapped[str] = mapped_column(Text, default="")
    proof: Mapped[dict] = mapped_column(default=dict)
    engine_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Set when a reviewer overturns a hard fail (appeal accepted or audit).
    overturned: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("submission_id", "rule"),)


class TestRequest(Base):
    """One HTTP request sent to a team's live link (the 'test_runs' table in the plan)."""
    __test__ = False  # not a pytest class

    __tablename__ = "test_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    test_id: Mapped[str] = mapped_column(String(128))
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    method: Mapped[str] = mapped_column(String(8))
    url: Mapped[str] = mapped_column(Text)
    request_body: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime]
    status_code: Mapped[int | None]
    latency_ms: Mapped[float | None]
    response_headers: Mapped[dict | None]
    response_body: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    target: Mapped[str] = mapped_column(String(16), default="live")  # live | sandbox


class TestResult(Base):
    __test__ = False  # not a pytest class
    __tablename__ = "test_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    target: Mapped[str] = mapped_column(String(16), default="live")
    test_id: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(64))
    weight: Mapped[float] = mapped_column(Float)
    passed: Mapped[bool] = mapped_column(Boolean)
    message: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[float] = mapped_column(Float, default=0)
    __table_args__ = (UniqueConstraint("submission_id", "attempt", "target", "test_id"),)


class TestAttempt(Base):
    __test__ = False  # not a pytest class
    __tablename__ = "test_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    target: Mapped[str] = mapped_column(String(16), default="live")
    is_retest: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    score: Mapped[float | None]  # 0-100
    passed: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    reachable: Mapped[bool | None]
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("submission_id", "attempt", "target"),)


class Score(Base):
    __tablename__ = "scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    component: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)  # 0-100 unless noted
    detail: Mapped[dict] = mapped_column(default=dict)
    engine_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (UniqueConstraint("submission_id", "component"),)


class LLMScore(Base):
    __tablename__ = "llm_scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    pass_no: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    criteria: Mapped[dict] = mapped_column(default=dict)
    value: Mapped[float | None]  # 0-100
    injection_suspected: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens_est: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (UniqueConstraint("submission_id", "pass_no"),)


class LLMBatch(Base):
    __tablename__ = "llm_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(256))
    pass_no: Mapped[int] = mapped_column(Integer)
    submission_ids: Mapped[list] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(32), default="submitted")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Flag(Base):
    __tablename__ = "flags"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    round: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(default=dict)
    related_submission_id: Mapped[int | None] = mapped_column(ForeignKey("submissions.id"))
    # open | upheld (disqualify) | dismissed | noted (reviewed, no action)
    status: Mapped[str] = mapped_column(String(16), default="open")
    reviewer: Mapped[str | None] = mapped_column(String(128))
    decision_note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (Index("ix_flag_dedupe", "submission_id", "type", "related_submission_id"),)


class Appeal(Base):
    __tablename__ = "appeals"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    round: Mapped[int] = mapped_column(Integer)
    # appeal: against a rule decision; retest: one live re-test request
    kind: Mapped[str] = mapped_column(String(16), default="appeal")
    token: Mapped[str] = mapped_column(String(64), unique=True)
    check_id: Mapped[int | None] = mapped_column(ForeignKey("checks.id"))
    flag_id: Mapped[int | None] = mapped_column(ForeignKey("flags.id"))
    expires_at: Mapped[datetime]
    # issued -> open (participant filed) -> accepted | rejected; retest: issued -> used
    status: Mapped[str] = mapped_column(String(16), default="issued")
    message: Mapped[str | None] = mapped_column(Text)
    filed_at: Mapped[datetime | None]
    reviewer: Mapped[str | None] = mapped_column(String(128))
    decision_note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Notice(Base):
    """Evidence reports waiting to be delivered. Email is out of scope for now:
    notices are written to the outbox folder and exported as CSV."""

    __tablename__ = "notices"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(Text)
    body_path: Mapped[str] = mapped_column(String(1024))
    links: Mapped[dict] = mapped_column(default=dict)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PlagiarismPair(Base):
    __tablename__ = "plagiarism_pairs"
    id: Mapped[int] = mapped_column(primary_key=True)
    a_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    b_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    minhash_est: Mapped[float] = mapped_column(Float)
    similarity: Mapped[float] = mapped_column(Float)  # containment of the smaller repo
    jplag_similarity: Mapped[float | None]
    method: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict] = mapped_column(default=dict)
    __table_args__ = (UniqueConstraint("a_id", "b_id"),)


class SandboxRun(Base):
    __tablename__ = "sandbox_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), unique=True)
    strategy: Mapped[str | None] = mapped_column(String(64))
    build_ok: Mapped[bool | None]
    run_ok: Mapped[bool | None]
    sandbox_score: Mapped[float | None]
    live_score: Mapped[float | None]
    verdict: Mapped[str | None] = mapped_column(String(32))  # consistent | mismatch | build_failed | manual
    log_path: Mapped[str | None] = mapped_column(String(1024))
    detail: Mapped[dict] = mapped_column(default=dict)
    spot_checked_by: Mapped[str | None] = mapped_column(String(128))
    spot_check_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class SandboxOverride(Base):
    """Manual build instructions for repos the auto-detector cannot build."""

    __tablename__ = "sandbox_overrides"
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), primary_key=True)
    dockerfile: Mapped[str | None] = mapped_column(Text)
    subdir: Mapped[str | None] = mapped_column(String(512))
    port: Mapped[int | None]
    env: Mapped[dict] = mapped_column(default=dict)


class UICheck(Base):
    __tablename__ = "ui_checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), unique=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    status_code: Mapped[int | None]
    title: Mapped[str | None] = mapped_column(Text)
    load_ms: Mapped[float | None]
    console_errors: Mapped[list] = mapped_column(default=list)
    screenshot_path: Mapped[str | None] = mapped_column(String(1024))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Judge(Base):
    __tablename__ = "judges"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    email: Mapped[str] = mapped_column(String(256), default="")
    passcode: Mapped[str] = mapped_column(String(64), default="")


class JudgeConflict(Base):
    __tablename__ = "judge_conflicts"
    id: Mapped[int] = mapped_column(primary_key=True)
    judge_id: Mapped[int] = mapped_column(ForeignKey("judges.id"))
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    note: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("judge_id", "submission_id"),)


class JudgeAssignment(Base):
    __tablename__ = "judge_assignments"
    id: Mapped[int] = mapped_column(primary_key=True)
    judge_id: Mapped[int] = mapped_column(ForeignKey("judges.id"))
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    round: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("judge_id", "submission_id", "round"),)


class JudgeScore(Base):
    __tablename__ = "judge_scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    judge_id: Mapped[int] = mapped_column(ForeignKey("judges.id"))
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    round: Mapped[int] = mapped_column(Integer)  # 5 = judging, 6 = finale
    criterion: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    __table_args__ = (UniqueConstraint("judge_id", "submission_id", "round", "criterion"),)


class Audit(Base):
    """Random re-check of automatic rejections (fairness commitment 5)."""

    __tablename__ = "audits"
    id: Mapped[int] = mapped_column(primary_key=True)
    round: Mapped[int] = mapped_column(Integer)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    check_id: Mapped[int | None] = mapped_column(ForeignKey("checks.id"))
    reason: Mapped[str] = mapped_column(Text, default="")
    # pending | correct | wrong
    verdict: Mapped[str] = mapped_column(String(16), default="pending")
    reviewer: Mapped[str | None] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None]
    __table_args__ = (UniqueConstraint("round", "submission_id"),)


class RoundState(Base):
    __tablename__ = "round_state"
    round: Mapped[int] = mapped_column(Integer, primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class HandScore(Base):
    """Human rubric scores used to calibrate the LLM before the event."""

    __tablename__ = "hand_scores"
    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    scorer: Mapped[str] = mapped_column(String(128), default="")
    criteria: Mapped[dict] = mapped_column(default=dict)
    __table_args__ = (UniqueConstraint("submission_id", "scorer"),)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(256), unique=True)
    payload: Mapped[dict] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class EventLog(Base):
    """Append-only log of operator and reviewer actions."""

    __tablename__ = "event_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    submission_id: Mapped[int | None]
    detail: Mapped[dict] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

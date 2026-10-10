"""Engine configuration, loaded from a YAML file (default: ./hackeval.yaml).

Every number the plan says must be published before the event (minimum commit
count, weights, tester identity, thresholds) lives here, so the published
rules and the running engine come from the same file.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class EventCfg(BaseModel):
    name: str = "Hackathon"
    start: datetime
    deadline: datetime
    # Public base URL of the appeal/re-test site (hackeval.web.public).
    public_base_url: str = "http://localhost:8000"
    # Replay mode (dry run on a past hackathon): the frozen commit is the last
    # commit on the default branch dated before the deadline, not today's head.
    replay: bool = False

    @field_validator("start", "deadline")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


class SnapshotCfg(BaseModel):
    clone_attempts: int = 3
    # Delays (minutes) before attempt 2, 3, ... -> 3 tries over 30 minutes.
    retry_delays_min: list[int] = [15, 15]
    clone_timeout_s: int = 900
    concurrency: int = 32
    recheck_after_hours: int = 24


class RulesCfg(BaseModel):
    min_commits: int = 5
    first_commit_share_flag: float = 0.70
    repo_size_limit_mb: int = 200
    # "flag" or "hard_fail" for repos over the published size limit.
    repo_size_action: str = "flag"
    duplicate_jaccard_flag: float = 0.80
    # Blobs shared by more than this many repos are boilerplate (templates,
    # licences, framework scaffolds) and ignored for duplicate detection.
    boilerplate_min_repos: int = 5
    code_extensions: list[str] = [
        ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rs", ".rb", ".php",
        ".cs", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift", ".dart", ".scala", ".vue",
        ".svelte", ".html", ".css", ".scss", ".sql", ".sh",
    ]
    ignore_dirs: list[str] = [
        "node_modules", "vendor", "dist", "build", ".next", "out", "venv", ".venv",
        "env", "__pycache__", "target", ".git", "coverage", ".expo", "Pods", "bower_components",
    ]
    ignore_files: list[str] = [
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
        "Cargo.lock", "composer.lock", "go.sum",
    ]


class TesterCfg(BaseModel):
    user_agent: str = "HackEval-Tester/1.0 (+https://example.org/hackathon/tester)"
    request_id_header: str = "X-Eval-Request-ID"
    published_ip: str = "0.0.0.0"
    attempts: int = 3
    spread_hours: float = 6.0
    warmup_timeout_s: float = 90.0
    request_timeout_s: float = 20.0
    per_host_concurrency: int = 2
    per_platform_concurrency: int = 8
    # Shared hosting suffixes: limits apply to the platform as a whole.
    platform_suffixes: list[str] = [
        "vercel.app", "onrender.com", "herokuapp.com", "netlify.app", "railway.app",
        "up.railway.app", "fly.dev", "pages.dev", "github.io", "replit.app", "glitch.me",
        "azurewebsites.net", "appspot.com", "web.app", "firebaseapp.com", "koyeb.app",
    ]
    skip_remaining_on_full_score: bool = True
    max_body_bytes: int = 65536
    retest_window_hours: int = 24


class WeightsCfg(BaseModel):
    tests: float = 0.50
    quality: float = 0.15
    authenticity: float = 0.15
    llm: float = 0.20


class PlagiarismCfg(BaseModel):
    shingle_k: int = 5
    winnow_window: int = 4
    num_perm: int = 128
    lsh_threshold: float = 0.5
    # Exact containment on winnowed fingerprints that sends a pair to JPlag.
    candidate_similarity: float = 0.5
    flag_similarity: float = 0.70
    # Fingerprints in more than this fraction of repos are boilerplate.
    boilerplate_df: float = 0.02
    jplag_jar: str | None = None
    java_bin: str = "java"


class LLMCfg(BaseModel):
    provider: str = "anthropic"  # anthropic | openai | mock
    model: str = "claude-haiku-5-5"
    top_n: int = 500
    token_cap: int = 30000
    second_pass_band: int = 50  # ranks cutoff +/- band are scored twice
    rubric_path: str | None = None
    batch: bool = True
    poll_interval_s: int = 60
    max_output_tokens: int = 2048


class RankingCfg(BaseModel):
    to_voting: int = 200
    cutoff_review_band: int = 20
    # Optional fixed number of voting slots per track, e.g. {"track-a": 70}.
    track_quotas: dict[str, int] = {}


class SandboxCfg(BaseModel):
    cpus: float = 2.0
    memory: str = "2g"
    pids_limit: int = 512
    build_timeout_s: int = 900
    start_timeout_s: int = 120
    build_network: bool = True  # dependency install usually needs network
    run_network: str = "hackeval-internal"  # an --internal docker network
    default_port: int = 8080
    integrity_gap_flag: float = 20.0  # live score - sandbox score, in points


class JudgingCfg(BaseModel):
    judges_per_team: int = 3
    criteria: dict[str, float] = {
        "technical_depth": 0.3, "product_and_ux": 0.25, "problem_fit": 0.25, "presentation": 0.2,
    }
    finale_criteria: dict[str, float] = {
        "live_change_request": 0.4, "demo_quality": 0.3, "technical_depth": 0.3,
    }
    scale_max: int = 10


class AuditCfg(BaseModel):
    sample_rate: float = 0.02
    max_error_rate: float = 0.01


class Settings(BaseModel):
    database_url: str = "sqlite:///data/hackeval.db"
    data_dir: Path = Path("data")
    tracks_dir: Path = Path("tracks")
    github_token_env: str = "GITHUB_TOKEN"
    appeal_window_hours: int = 24
    event: EventCfg
    snapshot: SnapshotCfg = Field(default_factory=SnapshotCfg)
    rules: RulesCfg = Field(default_factory=RulesCfg)
    tester: TesterCfg = Field(default_factory=TesterCfg)
    weights: WeightsCfg = Field(default_factory=WeightsCfg)
    plagiarism: PlagiarismCfg = Field(default_factory=PlagiarismCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    ranking: RankingCfg = Field(default_factory=RankingCfg)
    sandbox: SandboxCfg = Field(default_factory=SandboxCfg)
    judging: JudgingCfg = Field(default_factory=JudgingCfg)
    audit: AuditCfg = Field(default_factory=AuditCfg)

    @property
    def github_token(self) -> str | None:
        return os.environ.get(self.github_token_env)

    def path(self, *parts: str) -> Path:
        # Absolute, because git runs with -C <repo> and would resolve relative paths there.
        p = self.data_dir.resolve().joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p


_override: Settings | None = None


def config_path() -> Path:
    return Path(os.environ.get("HACKEVAL_CONFIG", "hackeval.yaml"))


def load_settings(path: Path | None = None) -> Settings:
    path = path or config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Config file {path} not found. Copy hackeval.example.yaml to hackeval.yaml "
            "or set HACKEVAL_CONFIG."
        )
    data = yaml.safe_load(path.read_text()) or {}
    return Settings.model_validate(data)


@lru_cache(maxsize=1)
def _cached() -> Settings:
    return load_settings()


def get_settings() -> Settings:
    return _override if _override is not None else _cached()


def set_settings(s: Settings | None) -> None:
    """Install settings programmatically (tests, embedding)."""
    global _override
    _override = s

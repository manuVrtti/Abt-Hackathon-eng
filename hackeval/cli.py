"""Command-line interface. Run ``hackeval --help`` for the full list.

Typical event-day sequence (see README for timings):

    hackeval import-submissions subs.csv
    hackeval freeze                 # at the deadline
    hackeval worker &               # processes all queued jobs
    hackeval r1 finalize            # after snapshots + rule jobs finish
    hackeval notices export outbox.csv
    hackeval r2 start               # live tests, full code, quality, fingerprints
    hackeval r2 plagiarism && hackeval r2 authenticity
    hackeval r2 llm-pass1 ; hackeval r2 llm-poll
    hackeval r2 llm-pass2 ; hackeval r2 llm-poll
    hackeval r2 rank
    hackeval voting export top200.csv --finalize
    hackeval voting import voted60.csv
    hackeval r4 start ; hackeval r4 advance --max 50
    hackeval judges assign --round 5 ; hackeval judges advance --round 5 --keep 10
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

import typer
from sqlalchemy import func, select

from . import db

app = typer.Typer(no_args_is_help=True, help="Hackathon evaluation engine")
r1_app = typer.Typer(no_args_is_help=True, help="Round 1: rule gates")
r2_app = typer.Typer(no_args_is_help=True, help="Round 2: automated scoring")
vote_app = typer.Typer(no_args_is_help=True, help="Round 3 hand-off to website voting")
r4_app = typer.Typer(no_args_is_help=True, help="Round 4: sandbox build and integrity audit")
judge_app = typer.Typer(no_args_is_help=True, help="Rounds 5-6: judging and finale")
audit_app = typer.Typer(no_args_is_help=True, help="Random audit of automatic rejections")
notice_app = typer.Typer(no_args_is_help=True, help="Evidence reports outbox")
llm_app = typer.Typer(no_args_is_help=True, help="LLM rubric calibration")
app.add_typer(r1_app, name="r1")
app.add_typer(r2_app, name="r2")
app.add_typer(vote_app, name="voting")
app.add_typer(r4_app, name="r4")
app.add_typer(judge_app, name="judges")
app.add_typer(audit_app, name="audit")
app.add_typer(notice_app, name="notices")
app.add_typer(llm_app, name="llm")


def out(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v")):
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@app.command("init-db")
def init_db():
    """Create database tables."""
    db.init_db()
    typer.echo("database ready")


@app.command("import-submissions")
def import_submissions(csv_path: Path, track: str = "default", update: bool = False):
    """Import the website's submission CSV (team_id, team_name, email, track, repo_url, live_url, submitted_at)."""
    from .intake import import_csv

    db.init_db()
    out(import_csv(csv_path, default_track=track, update=update))


@app.command()
def status():
    """Counts per round and status, open review items and the job queue."""
    from .jobs import job_counts
    from .models import Appeal, Flag, RoundState, Submission

    with db.session() as s:
        by = s.execute(select(Submission.round, Submission.status, func.count())
                       .group_by(Submission.round, Submission.status)).all()
        flags = s.scalar(select(func.count()).select_from(Flag).where(Flag.status == "open"))
        appeals = s.scalar(select(func.count()).select_from(Appeal).where(Appeal.status == "open"))
        paused = [(r.round, r.reason) for r in s.scalars(select(RoundState).where(RoundState.paused.is_(True)))]
    out({"submissions": [{"round": r, "status": st, "count": n} for r, st, n in sorted(by)],
         "open_flags": flags, "open_appeals": appeals, "paused_rounds": paused,
         "jobs": [{"kind": k, "status": st, "count": n} for k, st, n in sorted(job_counts())]})


@app.command()
def worker(concurrency: int = 32, kinds: str = typer.Option("", help="comma-separated job kinds"),
           drain: bool = typer.Option(False, help="exit when no job is ready"),
           wait_scheduled: bool = typer.Option(False, help="with --drain, also wait for scheduled jobs")):
    """Run job workers (snapshots, rules, live tests, quality, fingerprints, sandbox ...)."""
    from .jobs import run_worker

    db.init_db()
    stats = asyncio.run(run_worker(concurrency, [k for k in kinds.split(",") if k] or None, drain=drain,
                                   wait_scheduled=wait_scheduled))
    typer.echo(f"processed {stats.processed} jobs")


@app.command()
def freeze():
    """R0: record every repo's head commit at the deadline and queue snapshot jobs."""
    from .snapshot import freeze_heads

    db.init_db()
    out(freeze_heads())


# ------------------------------------------------------------------ R1
@r1_app.command("finalize")
def r1_finalize():
    """Cross-team duplicate checks, then disqualify hard fails (with evidence) and advance the rest."""
    from .rules import apply_r1_decisions, cross_team_checks
    from .review import ensure_round_open

    ensure_round_open(1)
    out({"cross_team_flags": cross_team_checks(), "decisions": apply_r1_decisions()})


@r1_app.command("rerun")
def r1_rerun():
    """Re-queue every R1 rule job (after a fix; results are replaced, reviewer overturns kept)."""
    from . import jobs
    from .models import Submission

    with db.session() as s:
        n = 0
        for sub in s.scalars(select(Submission).where(Submission.round >= 1)):
            jobs.enqueue(s, "r1_rules", str(sub.id), {"submission_id": sub.id}, requeue=True)
            n += 1
    typer.echo(f"re-queued {n} rule jobs")


# ------------------------------------------------------------------ R2
@r2_app.command("start")
def r2_start():
    """Queue full-code fetch (then quality + fingerprints) and 3 live test attempts per R1 survivor."""
    from .r2 import start_r2

    typer.echo(f"queued R2 work for {start_r2()} teams")


@r2_app.command("plagiarism")
def r2_plagiarism():
    """Find similar pairs across teams (after fingerprint jobs finish) and flag them."""
    from .plagiarism import run_plagiarism

    out(run_plagiarism())


@r2_app.command("authenticity")
def r2_authenticity():
    """Compute the development-authenticity score (commit pattern + plagiarism)."""
    from .r2 import compute_authenticity

    typer.echo(f"scored {compute_authenticity()} teams")


@r2_app.command("llm-pass1")
def r2_llm_pass1():
    """Submit the LLM rubric for the top llm.top_n teams of the preliminary ranking."""
    from .llm.scoring import run_pass1

    out(run_pass1())


@r2_app.command("llm-pass2")
def r2_llm_pass2():
    """Score teams near the cut-off a second time (averaged with pass 1)."""
    from .llm.scoring import run_pass2

    out(run_pass2())


@r2_app.command("llm-poll")
def r2_llm_poll(wait: bool = False):
    """Collect finished LLM batches. With --wait, keep polling until all are done."""
    import time

    from .config import get_settings
    from .llm.scoring import poll_batches

    while True:
        res = poll_batches()
        out(res)
        if not wait or res["pending"] == 0:
            break
        time.sleep(get_settings().llm.poll_interval_s)


@r2_app.command("llm-retry")
def r2_llm_retry(pass_no: int = 1):
    """Re-score (synchronously) teams whose LLM result failed or could not be parsed."""
    from .llm.scoring import retry_failed

    out(retry_failed(pass_no))


@r2_app.command("ui-checks")
def r2_ui_checks(top: int = 0):
    """Queue Playwright screenshot/console checks for the top teams."""
    from .uicheck import enqueue_ui_checks

    typer.echo(f"queued {enqueue_ui_checks(top or None)} UI checks")


@r2_app.command("rank")
def r2_rank(csv_path: Path = typer.Option(None, "--csv"), show: int = 30):
    """Compute the weighted ranking and flag the teams around the cut-off for review."""
    from .ranking import COMPONENTS, final_ranking, flag_cutoff_band

    rows = final_ranking()
    flagged = flag_cutoff_band(rows)
    if csv_path:
        import csv

        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["rank", "team_id", "track", "total", *COMPONENTS, "open_flags"])
            for r in rows:
                w.writerow([r.rank, r.team_id, r.track, r.total, *[r.comps[c] for c in COMPONENTS],
                            ";".join(r.open_flags)])
    for r in rows[:show]:
        typer.echo(f"{r.rank:>4}  {r.team_id:<16} {r.total:7.2f}  " +
                   "  ".join(f"{c}={r.comps[c] if r.comps[c] is not None else '-'}" for c in COMPONENTS))
    typer.echo(f"ranked {len(rows)} teams; {flagged} flagged for cut-off review")


# ------------------------------------------------------------------ R3
@vote_app.command("export")
def vote_export(csv_path: Path, finalize: bool = False):
    """Export the top teams for website voting. --finalize moves them to R3 and eliminates the rest."""
    from .ranking import export_voting

    typer.echo(f"exported {export_voting(csv_path, finalize)} teams to {csv_path}")


@vote_app.command("import")
def vote_import(csv_path: Path):
    """Import the teams that came back from voting (team_id[, votes])."""
    from .ranking import import_voted

    out(import_voted(csv_path))


# ------------------------------------------------------------------ R4
@r4_app.command("start")
def r4_start():
    """Queue sandbox build + integrity comparison for every R4 team."""
    from .sandbox import start_r4

    typer.echo(f"queued sandbox runs for {start_r4()} teams")


@r4_app.command("advance")
def r4_advance(max_teams: int = typer.Option(50, "--max"), order_by: str = "total"):
    """Move up to --max cleared teams to judging (R5)."""
    from .review import ensure_round_open
    from .sandbox import advance_r4

    ensure_round_open(4)
    out(advance_r4(max_teams, order_by))


# ------------------------------------------------------------------ R5/R6
@judge_app.command("import")
def judges_import(csv_path: Path):
    """Import judges (name, email, conflicts as team_ids separated by ';')."""
    from .judging import import_judges

    typer.echo(f"imported {import_judges(csv_path)} judges")


@judge_app.command("assign")
def judges_assign(round_: int = typer.Option(5, "--round"), per_team: int = 0):
    """Assign judges to teams (skipping declared conflicts)."""
    from .judging import assign

    out(assign(round_, per_team or None))


@judge_app.command("results")
def judges_results(round_: int = typer.Option(5, "--round")):
    """Normalised judging results."""
    from .judging import results

    out(results(round_))


@judge_app.command("advance")
def judges_advance(round_: int = typer.Option(5, "--round"), keep: int = 10):
    """R5: move the top --keep teams to the finale. R6: record final placings."""
    from .judging import advance

    out(advance(round_, keep))


# ------------------------------------------------------------------ audit
@audit_app.command("sample")
def audit_sample(round_: int = typer.Option(..., "--round"), seed: int = typer.Option(None)):
    """Randomly sample automatic rejections of a round for human re-checking."""
    from .audit import sample

    typer.echo(f"sampled {sample(round_, seed=seed)} rejections")


@audit_app.command("status")
def audit_status(round_: int = typer.Option(..., "--round")):
    from .audit import evaluate

    out(evaluate(round_))


@audit_app.command("resume")
def audit_resume(round_: int = typer.Option(..., "--round"), reason: str = "fixed"):
    """Un-pause a round after the cause of audit errors is fixed."""
    from .review import set_paused

    set_paused(round_, False, reason)
    typer.echo(f"round {round_} resumed")


# ------------------------------------------------------------------ notices
@notice_app.command("export")
def notices_export(csv_path: Path, all_: bool = typer.Option(False, "--all"), mark: bool = True):
    """Write undelivered evidence reports (with appeal / re-test links) to a CSV for sending."""
    from .notices import export_outbox

    with db.session() as s:
        n = export_outbox(s, csv_path, only_new=not all_, mark=mark)
    typer.echo(f"exported {n} notices to {csv_path}")


# ------------------------------------------------------------------ LLM calibration
@llm_app.command("import-hand-scores")
def llm_import_hand(csv_path: Path):
    """Import human rubric scores (team_id, scorer, <criterion columns 1-5>)."""
    from .llm.calibrate import import_hand_scores

    typer.echo(f"imported {import_hand_scores(csv_path)} hand scores")


@llm_app.command("calibrate")
def llm_calibrate(skip_llm: bool = False):
    """Score the hand-scored entries with the LLM and report agreement."""
    from .llm.calibrate import calibrate

    out(calibrate(run_llm=not skip_llm))


# ------------------------------------------------------------------ replay / dry run
@app.command("replay-compare")
def replay_compare(outcomes_csv: Path):
    """Compare engine results with a past hackathon's real outcomes (dry run)."""
    from .replay import compare

    out(compare(outcomes_csv))


# ------------------------------------------------------------------ participants
@app.command()
def selfcheck(url: str, track: str = typer.Option(..., "--track"), hidden: bool = False):
    """Run a track's public sample tests (or --hidden tests, organisers only) against a URL."""
    from .livetest.framework import run_suite
    from .livetest.runner import identity
    from .tracks import get_track

    t = get_track(track)
    tests = t.load_hidden() if hidden else t.load_public()
    res = asyncio.run(run_suite(tests, url, identity(), warmup_path=t.warmup_path))
    typer.echo(f"reachable: {res.reachable}")
    for o in res.outcomes:
        typer.echo(f"  [{'PASS' if o.passed else 'FAIL'}] {o.test_id:<30} {o.message}")
    typer.echo(f"score: {res.score}/100")


@app.command("public-kit")
def public_kit(outdir: Path):
    """Build the participant kit: rule-check script, sample tests per track, test runner, published rules."""
    from .config import get_settings
    from .tracks import get_track, list_tracks

    cfg = get_settings()
    outdir.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parent
    shutil.copy(root.parent / "public" / "rulecheck.py", outdir / "rulecheck.py")
    shutil.copy(root / "livetest" / "framework.py", outdir / "contract_framework.py")
    for tid in list_tracks():
        t = get_track(tid)
        d = outdir / "tracks" / tid
        d.mkdir(parents=True, exist_ok=True)
        if t.public_tests:
            text = t.public_tests.read_text().replace("from hackeval.livetest.framework import",
                                                      "from contract_framework import")
            (d / "sample_tests.py").write_text(text)
        (d / "problem_statement.md").write_text(f"# {t.name}\n\n{t.problem_statement}\n")
    rules = {
        "event": cfg.event.name, "start": cfg.event.start, "deadline": cfg.event.deadline,
        "min_commits": cfg.rules.min_commits, "first_commit_share_flag": cfg.rules.first_commit_share_flag,
        "repo_size_limit_mb": cfg.rules.repo_size_limit_mb, "weights": cfg.weights.model_dump(),
        "tester": {"ip": cfg.tester.published_ip, "user_agent": cfg.tester.user_agent,
                   "request_id_header": cfg.tester.request_id_header, "attempts": cfg.tester.attempts,
                   "spread_hours": cfg.tester.spread_hours, "warmup_timeout_s": cfg.tester.warmup_timeout_s},
        "appeal_window_hours": cfg.appeal_window_hours,
        "audit": cfg.audit.model_dump(),
    }
    (outdir / "published_rules.json").write_text(json.dumps(rules, indent=2, default=str))
    from .llm.rubric import load_rubric
    import yaml

    rub = load_rubric(cfg.llm.rubric_path)
    (outdir / "rubric.yaml").write_text(yaml.safe_dump(
        {"criteria": {c.key: {"weight": c.weight, "question": c.question, "levels": c.levels}
                      for c in rub.criteria}}, sort_keys=False))
    typer.echo(f"kit written to {outdir}")


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000):
    """Serve the public appeal / re-test pages."""
    import uvicorn

    uvicorn.run("hackeval.web.public:app", host=host, port=port)


@app.command()
def backup(dest: Path = typer.Option(None, help="directory for the dump")):
    """Dump the database (pg_dump for PostgreSQL, file copy for SQLite)."""
    from .config import get_settings
    from .util import run, utcnow

    cfg = get_settings()
    dest = dest or cfg.path("backups")
    dest.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    url = cfg.database_url
    if url.startswith("sqlite"):
        target = dest / f"hackeval-{stamp}.db"
        with db.engine().connect() as conn:
            conn.exec_driver_sql(f"VACUUM INTO '{target}'")
    else:
        target = dest / f"hackeval-{stamp}.dump"
        pg_url = url.replace("postgresql+psycopg://", "postgresql://")
        r = run(["pg_dump", "--format=custom", f"--file={target}", pg_url], timeout=3600)
        if r.code != 0:
            raise typer.Exit(f"pg_dump failed: {r.err}")
    typer.echo(str(target))


if __name__ == "__main__":
    app()

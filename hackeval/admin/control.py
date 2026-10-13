"""Organiser pages of the admin app: submissions upload, problem statements
(tracks) and their tests, rules and settings, and buttons for every round.
Everything here calls the same functions as the `hackeval` command line."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml
from sqlalchemy import func, select

from hackeval import db
from hackeval.config import get_settings, save_settings
from hackeval.jobs import job_counts
from hackeval.models import Submission


def _upload_to_temp(f) -> Path:
    tmp = Path(tempfile.mkdtemp()) / f.name
    tmp.write_bytes(f.getvalue())
    return tmp


def _frozen() -> bool:
    with db.session() as s:
        return bool(s.scalar(select(func.count()).select_from(Submission).where(Submission.frozen_sha.is_not(None))))


def _run(label: str, fn, *args, key: str, help: str = "", primary: bool = False, **kw):
    """A button that runs one engine step and shows its result or error."""
    if st.button(label, key=key, help=help, type="primary" if primary else "secondary"):
        try:
            with st.spinner(f"{label}..."):
                res = fn(*args, **kw)
            st.success(f"{label}: done")
            if res is not None:
                st.json(res if isinstance(res, (dict, list)) else {"result": res}, expanded=False)
            return res
        except Exception as e:  # show any engine error (e.g. a paused round) in the page
            st.error(f"{label} failed: {e}")
    return None


# ------------------------------------------------------------------ submissions
def page_submissions(reviewer: str):
    from hackeval.intake import import_csv
    from hackeval.tracks import list_tracks

    st.header("Submissions")
    st.write("Upload the submission export from the website. Columns are matched by name: "
             "`team_id`, `team_name`, `email`, `track` (problem statement), `repo_url`, `live_url`, `submitted_at`. "
             "Re-upload as often as you like before the deadline; existing teams are kept.")
    f = st.file_uploader("Submissions CSV", type=["csv"])
    update = st.checkbox("Update details of teams already imported (only before the freeze)")
    if f is not None and st.button("Import", type="primary"):
        try:
            db.init_db()
            res = import_csv(_upload_to_temp(f), update=update)
            db_log(reviewer, "submissions_imported", **res)
            st.success(f"Imported: {res}")
        except ValueError as e:
            st.error(str(e))

    with db.session() as s:
        subs = s.scalars(select(Submission).order_by(Submission.id)).all()
    if not subs:
        st.info("No submissions yet.")
        return
    known = set(list_tracks())
    df = pd.DataFrame([{"team_id": x.team_id, "team_name": x.team_name, "track": x.track, "repo_url": x.repo_url,
                        "live_url": x.live_url, "status": x.status, "round": x.round} for x in subs])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Teams", len(subs))
    c2.metric("Not a GitHub URL", sum(1 for x in subs if not x.repo_owner))
    c3.metric("No live link", sum(1 for x in subs if not x.live_url))
    c4.metric("Unknown track", sum(1 for x in subs if x.track not in known))
    if any(x.track not in known for x in subs):
        st.warning("Some teams name a track (problem statement) that has no tests yet: "
                   + ", ".join(sorted({x.track for x in subs if x.track not in known}))
                   + ". Add it on the Problem statements page before Round 2.")
    st.dataframe(df.groupby("track").size().rename("teams").reset_index(), hide_index=True)
    st.dataframe(df, hide_index=True, height=400)


def db_log(actor: str, action: str, **detail) -> None:
    with db.session() as s:
        db.log_event(s, actor, action, None, **detail)


# ------------------------------------------------------------------ tracks
def page_tracks(reviewer: str):
    from hackeval import tracks as tr
    from hackeval.livetest.framework import load_suite, run_suite
    from hackeval.livetest.runner import identity

    st.header("Problem statements and tests")
    st.write("Each problem statement is a *track*. Teams pick one in the `track` column of the submission CSV. "
             "A track has the problem statement (also given to the LLM judge), a public sample test file and the "
             "**hidden** contract tests that score 50% of Round 2.")
    root = get_settings().tracks_dir
    ids = tr.list_tracks()
    choice = st.selectbox("Track", ["(new track)", *ids])
    cur = tr.get_track(choice) if choice != "(new track)" else None

    with st.form("track"):
        tid = st.text_input("Track ID (used in the CSV, no spaces)", value=cur.id if cur else "",
                            disabled=cur is not None)
        name = st.text_input("Name", value=cur.name if cur else "")
        statement = st.text_area("Problem statement, including the exact API contract teams must implement",
                                 value=cur.problem_statement if cur else "", height=260)
        warmup = st.text_input("Warm-up path (first request, waits up to 90 s for sleeping servers)",
                               value=cur.warmup_path if cur else "/")
        hidden = st.file_uploader("Hidden tests (.py) - keep private", type=["py"])
        public = st.file_uploader("Public sample tests (.py) - published to teams", type=["py"])
        saved = st.form_submit_button("Save track", type="primary")
    if saved:
        tid = (cur.id if cur else tid).strip()
        if not tid or not tid.replace("-", "").replace("_", "").isalnum():
            st.error("Track ID must use letters, numbers, - or _.")
            return
        d = root / tid
        d.mkdir(parents=True, exist_ok=True)
        for upload, fname in ((hidden, "hidden_tests.py"), (public, "public_tests.py")):
            if upload is not None:
                tmp = _upload_to_temp(upload)
                try:
                    tests = load_suite(tmp)
                except Exception as e:
                    st.error(f"{upload.name} does not load: {e}")
                    return
                if not tests:
                    st.error(f"{upload.name} has no @contract_test functions.")
                    return
                shutil.copy(tmp, d / fname)
        spec = {"name": name or tid, "warmup_path": warmup or "/", "public_tests": "public_tests.py",
                "hidden_tests": "hidden_tests.py", "problem_statement": statement}
        (d / "track.yaml").write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True), encoding="utf-8")
        tr._load.cache_clear()
        db_log(reviewer, "track_saved", track=tid)
        st.success(f"Saved track {tid}.")
        st.rerun()

    if cur is None:
        return
    for label, loader in (("Hidden tests", cur.load_hidden), ("Public sample tests", cur.load_public)):
        try:
            tests = loader()
            st.subheader(f"{label} ({len(tests)})")
            st.dataframe(pd.DataFrame([{"order": i + 1, "id": t.id, "category": t.category, "weight": t.weight,
                                        "description": t.description} for i, t in enumerate(tests)]),
                         hide_index=True)
        except Exception as e:
            st.error(f"{label}: {e}")
    st.subheader("Try the hidden tests against a URL")
    st.caption("Run them against your own reference solution before the event: every test must pass on a "
               "correct implementation, or the suite is unfair.")
    url = st.text_input("Base URL", key="trial_url")
    if url and st.button("Run hidden tests"):
        with st.spinner("Running..."):
            res = asyncio.run(run_suite(cur.load_hidden(), url, identity(), warmup_path=cur.warmup_path))
        st.metric("Score", f"{res.score}/100")
        st.dataframe(pd.DataFrame([{"test": o.test_id, "passed": o.passed, "message": o.message}
                                   for o in res.outcomes]), hide_index=True)
        st.dataframe(pd.DataFrame([{"request_id": r.request_id, "test": r.test_id, "method": r.method, "url": r.url,
                                    "status": r.status_code, "ms": r.latency_ms, "server_ip": r.remote_ip,
                                    "error": r.error} for r in res.records]), hide_index=True)


# ------------------------------------------------------------------ rules
def _dt_input(label: str, value: datetime, key: str) -> datetime | None:
    txt = st.text_input(label + " (ISO, e.g. 2026-11-01T09:00:00+05:30)", value=value.isoformat(), key=key)
    try:
        d = datetime.fromisoformat(txt)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        st.error(f"{label}: not a valid date-time")
        return None


def page_rules(reviewer: str):
    cfg = get_settings()
    st.header("Rules and settings")
    st.write("These values decide every round. Everything here is **published to participants** in the kit "
             "before the event, so the rules teams read and the rules the engine applies are the same file.")
    if _frozen():
        st.warning("Submissions are already frozen. Changing rules now changes them mid-event; every change is "
                   "logged with old and new values.")
    with st.form("rules"):
        st.subheader("Event")
        name = st.text_input("Event name", cfg.event.name)
        start = _dt_input("Hackathon start", cfg.event.start, "start")
        deadline = _dt_input("Submission deadline", cfg.event.deadline, "deadline")
        base = st.text_input("Public site URL (appeals, re-tests, tester check)", cfg.event.public_base_url)
        appeal = st.number_input("Appeal window (hours, at least 12)", 12, 72, cfg.appeal_window_hours)

        st.subheader("Round 1 rules")
        min_commits = st.number_input("Minimum commits", 1, 100, cfg.rules.min_commits)
        dump = st.slider("Flag if the first commit holds more than this share of the final code", 0.3, 1.0,
                         cfg.rules.first_commit_share_flag, 0.05)
        size = st.number_input("Repository size limit (MB)", 10, 5000, cfg.rules.repo_size_limit_mb)
        size_action = st.selectbox("Over the size limit", ["flag", "hard_fail"],
                                   index=["flag", "hard_fail"].index(cfg.rules.repo_size_action))

        st.subheader("Round 2 live tester (published identity)")
        ip = st.text_input("Tester IP address (the server's fixed IP)", cfg.tester.published_ip)
        ua = st.text_input("Tester User-Agent", cfg.tester.user_agent)
        attempts = st.number_input("Attempts", 1, 6, cfg.tester.attempts)
        spread = st.number_input("Spread attempts over (hours)", 0.0, 24.0, float(cfg.tester.spread_hours))
        warm = st.number_input("First-request wait for sleeping servers (seconds)", 1, 300,
                               int(cfg.tester.warmup_timeout_s))

        st.subheader("Round 2 weights (must add up to 100%)")
        c1, c2, c3, c4 = st.columns(4)
        w_t = c1.number_input("Hidden tests %", 0, 100, round(cfg.weights.tests * 100))
        w_q = c2.number_input("Code quality %", 0, 100, round(cfg.weights.quality * 100))
        w_a = c3.number_input("Authenticity %", 0, 100, round(cfg.weights.authenticity * 100))
        w_l = c4.number_input("LLM rubric %", 0, 100, round(cfg.weights.llm * 100))

        st.subheader("Shortlists")
        top_n = st.number_input("Teams scored by the LLM rubric", 10, 5000, cfg.llm.top_n)
        to_voting = st.number_input("Teams sent to voting", 1, 2000, cfg.ranking.to_voting)
        band = st.number_input("Review band around the cut-off (places)", 0, 200, cfg.ranking.cutoff_review_band)

        st.subheader("LLM judge")
        provider = st.selectbox("Provider", ["anthropic", "openai", "mock"],
                                index=["anthropic", "openai", "mock"].index(cfg.llm.provider))
        model = st.text_input("Model", cfg.llm.model)
        save = st.form_submit_button("Save rules", type="primary")
    if save:
        if None in (start, deadline):
            return
        if w_t + w_q + w_a + w_l != 100:
            st.error(f"Weights add up to {w_t + w_q + w_a + w_l}%, not 100%.")
            return
        if deadline <= start:
            st.error("The deadline must be after the start.")
            return
        new = cfg.model_copy(deep=True)
        new.event.name, new.event.start, new.event.deadline, new.event.public_base_url = name, start, deadline, base
        new.appeal_window_hours = int(appeal)
        new.rules.min_commits, new.rules.first_commit_share_flag = int(min_commits), float(dump)
        new.rules.repo_size_limit_mb, new.rules.repo_size_action = int(size), size_action
        new.tester.published_ip, new.tester.user_agent = ip, ua
        new.tester.attempts, new.tester.spread_hours, new.tester.warmup_timeout_s = int(attempts), float(spread), float(warm)
        new.weights.tests, new.weights.quality = w_t / 100, w_q / 100
        new.weights.authenticity, new.weights.llm = w_a / 100, w_l / 100
        new.llm.top_n, new.llm.provider, new.llm.model = int(top_n), provider, model
        new.ranking.to_voting, new.ranking.cutoff_review_band = int(to_voting), int(band)
        old_d, new_d = cfg.model_dump(mode="json"), new.model_dump(mode="json")
        diff = {k: {"old": old_d[k], "new": new_d[k]} for k in new_d if old_d[k] != new_d[k]}
        path = save_settings(new)
        db_log(reviewer, "rules_changed", changes=diff)
        st.success(f"Saved to {path}. Restart the worker service so background jobs use the new rules.")
        st.rerun()

    st.subheader("Participant kit")
    st.write("Rule-check script, sample tests and problem statement per track, the published rules and the rubric. "
             "Hidden tests are never included.")
    if st.button("Build participant kit"):
        from hackeval.cli import public_kit

        out = Path(tempfile.mkdtemp()) / "participant-kit"
        public_kit(out)
        zip_path = shutil.make_archive(str(out), "zip", out)
        st.download_button("Download participant-kit.zip", Path(zip_path).read_bytes(), "participant-kit.zip")
        st.json(json.loads((out / "published_rules.json").read_text()), expanded=False)


# ------------------------------------------------------------------ rounds
def page_rounds(reviewer: str):
    from hackeval import audit, judging, ranking, sandbox
    from hackeval.llm import scoring
    from hackeval.notices import export_outbox
    from hackeval.plagiarism import run_plagiarism
    from hackeval.r2 import compute_authenticity, start_r2
    from hackeval.review import ensure_round_open
    from hackeval.rules import apply_r1_decisions, cross_team_checks
    from hackeval.snapshot import freeze_heads

    st.header("Run the rounds")
    st.info("Background work (cloning, rule checks, live tests, code checks, sandbox builds) is done by the "
            "**worker** service, which must be running: `hackeval worker`. Buttons queue work or run quick steps; "
            "watch progress in the job queue below.")
    st.dataframe(pd.DataFrame(job_counts(), columns=["kind", "status", "jobs"]), hide_index=True)

    def r1_finalize():
        ensure_round_open(1)
        return {"cross_team_flags": cross_team_checks(), "decisions": apply_r1_decisions()}

    def rank():
        rows = ranking.final_ranking()
        return {"ranked": len(rows), "flagged_for_cutoff_review": ranking.flag_cutoff_band(rows)}

    def notices():
        out = get_settings().path("outbox") / f"notices-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.csv"
        with db.session() as s:
            n = export_outbox(s, out)
        return {"exported": n, "file": str(out)}

    with st.expander("Round 0 · Freeze at the deadline", expanded=True):
        st.write("Records every repository's latest commit (only that version is judged), then the worker "
                 "clones them and runs the Round 1 rules automatically.")
        _run("Freeze all repositories", freeze_heads, key="freeze", primary=True)
    with st.expander("Round 1 · Rule gates"):
        st.write("After the clones finish: duplicate checks across teams, then hard fails are disqualified "
                 "with evidence and an appeal link; flags go to the Flag queue; everyone else advances.")
        _run("Finalize Round 1", r1_finalize, key="r1")
        _run("Sample 2% of rejections for audit", audit.sample, 1, key="a1")
        _run("Export evidence reports (CSV of links to send)", notices, key="n1")
    with st.expander("Round 2 · Automated scoring"):
        _run("1. Start: fetch code, quality checks, 3 live test attempts", start_r2, key="r2s")
        _run("2. Plagiarism across teams", run_plagiarism, key="plag")
        _run("3. Authenticity scores", compute_authenticity, key="auth")
        _run("4. LLM rubric, first pass (top teams)", scoring.run_pass1, key="llm1")
        _run("5. Collect LLM results", scoring.poll_batches, key="poll1")
        _run("6. LLM second pass near the cut-off", scoring.run_pass2, key="llm2")
        _run("7. Collect LLM results", scoring.poll_batches, key="poll2")
        _run("8. Rank and flag the cut-off band for review", rank, key="rank")
        _run("Export test logs and re-test links (CSV)", notices, key="n2")
    with st.expander("Round 3 · Voting hand-off"):
        st.write("Clear all cut-off flags first. Export moves the top teams to voting and eliminates the rest.")
        if st.button("Export voting CSV and finalize Round 2"):
            try:
                out = get_settings().path("exports") / "voting.csv"
                n = ranking.export_voting(out, finalize=True)
                st.success(f"{n} teams exported.")
                st.download_button("Download voting.csv", out.read_bytes(), "voting.csv")
            except Exception as e:
                st.error(str(e))
        voted = st.file_uploader("Voted teams CSV from the website (team_id, votes)", type=["csv"], key="voted")
        if voted is not None:
            _run("Import voted teams", ranking.import_voted, _upload_to_temp(voted), key="import_voted")
    with st.expander("Round 4 · Sandbox build and integrity audit"):
        _run("Queue sandbox builds", sandbox.start_r4, key="r4s")
        mx = st.number_input("Teams to send to judging", 1, 200, 50)
        _run("Advance cleared teams to judging", sandbox.advance_r4, int(mx), key="r4a")
    with st.expander("Rounds 5-6 · Judging and finale"):
        jf = st.file_uploader("Judges CSV (name, email, conflicts as team_ids separated by ;)", type=["csv"])
        if jf is not None:
            _run("Import judges", judging.import_judges, _upload_to_temp(jf), key="judges")
        rnd = st.selectbox("Round", [5, 6])
        _run("Assign judges", judging.assign, rnd, key="assign")
        keep = st.number_input("Teams to keep (finale)", 1, 50, 10)
        _run("Advance top teams", judging.advance, rnd, int(keep), key="jadv")

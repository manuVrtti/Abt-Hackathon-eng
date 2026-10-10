"""Admin page (Streamlit): flag queue, appeals, team lookup, ranking, audit,
sandbox spot-checks and judge scoring forms.

    HACKEVAL_ADMIN_PASSWORD=... streamlit run hackeval/admin/app.py

Reviewers sign in with the admin password and their name (recorded on every
decision). Judges sign in with their name and personal passcode and only see
the scoring form for teams assigned to them.
"""

from __future__ import annotations

import hmac
import os

import pandas as pd
import streamlit as st
from sqlalchemy import func, select

from hackeval import db
from hackeval.audit import evaluate as audit_eval, record as audit_record
from hackeval.config import get_settings
from hackeval.judging import criteria_for, record_score, results as judge_results
from hackeval.jobs import job_counts
from hackeval.models import (
    Appeal, Audit, Check, Flag, Judge, JudgeAssignment, JudgeConflict, JudgeScore, LLMScore, Notice, RoundState,
    SandboxOverride, SandboxRun, Score, Submission, TestAttempt, TestRequest, TestResult, UICheck,
)
from hackeval.review import decide_appeal, decide_flag, set_paused

st.set_page_config(page_title="HackEval admin", layout="wide")
cfg = get_settings()
db.init_db()


def login() -> tuple[str, str | int] | None:
    if "who" in st.session_state:
        return st.session_state["who"]
    st.title(f"{cfg.event.name}: evaluation admin")
    mode = st.radio("Sign in as", ["Reviewer / organiser", "Judge"], horizontal=True)
    name = st.text_input("Your name")
    secret = st.text_input("Password" if mode.startswith("Rev") else "Judge passcode", type="password")
    if st.button("Sign in") and name:
        if mode.startswith("Rev"):
            expected = os.environ.get("HACKEVAL_ADMIN_PASSWORD", "")
            if expected and hmac.compare_digest(secret, expected):
                st.session_state["who"] = ("reviewer", name)
                st.rerun()
            st.error("Wrong password (set HACKEVAL_ADMIN_PASSWORD on the server).")
        else:
            with db.session() as s:
                j = s.scalars(select(Judge).where(Judge.name == name)).first()
            if j and j.passcode and hmac.compare_digest(secret, j.passcode):
                st.session_state["who"] = ("judge", j.id)
                st.rerun()
            st.error("Unknown judge or wrong passcode.")
    return None


def team_label(sub: Submission) -> str:
    return f"{sub.team_id} · {sub.team_name}" if sub.team_name else sub.team_id


# ------------------------------------------------------------------ pages
def page_dashboard(_reviewer: str):
    st.header("Dashboard")
    with db.session() as s:
        rows = s.execute(select(Submission.round, Submission.status, func.count())
                         .group_by(Submission.round, Submission.status)).all()
        nflags = s.scalar(select(func.count()).select_from(Flag).where(Flag.status == "open"))
        nappeals = s.scalar(select(func.count()).select_from(Appeal).where(Appeal.status == "open"))
        paused = s.scalars(select(RoundState).where(RoundState.paused.is_(True))).all()
    c1, c2, c3 = st.columns(3)
    c1.metric("Submissions", sum(n for *_, n in rows))
    c2.metric("Open flags", nflags)
    c3.metric("Open appeals", nappeals)
    for p in paused:
        st.error(f"Round {p.round} is paused: {p.reason}")
    st.subheader("Teams by round and status")
    st.dataframe(pd.DataFrame(rows, columns=["round", "status", "teams"]), hide_index=True)
    st.subheader("Job queue")
    st.dataframe(pd.DataFrame(job_counts(), columns=["kind", "status", "jobs"]), hide_index=True)


def page_flags(reviewer: str):
    st.header("Flag queue")
    with db.session() as s:
        types = sorted({t for (t,) in s.execute(select(Flag.type).where(Flag.status == "open").distinct())})
    ftype = st.selectbox("Type", ["(all)", *types])
    with db.session() as s:
        q = select(Flag).where(Flag.status == "open").order_by(Flag.round, Flag.type, Flag.id)
        if ftype != "(all)":
            q = q.where(Flag.type == ftype)
        flags = s.scalars(q.limit(50)).all()
        subs = {x.id: x for x in s.scalars(select(Submission))}
    st.caption(f"Showing {len(flags)} open flags (50 at a time). No flag removes a team unless you uphold it.")
    for f in flags:
        sub = subs[f.submission_id]
        with st.expander(f"#{f.id} · R{f.round} · {f.type} · {team_label(sub)}"):
            st.write(f.reason)
            if f.related_submission_id:
                other = subs.get(f.related_submission_id)
                st.write(f"Related team: **{team_label(other)}** · {other.repo_url}")
            st.write(f"Repo: {sub.repo_url} · live: {sub.live_url} · frozen: `{sub.frozen_sha}`")
            st.json(f.evidence, expanded=False)
            note = st.text_input("Note", key=f"fn{f.id}")
            c1, c2, c3 = st.columns(3)
            if c1.button("Uphold (disqualify)", key=f"fu{f.id}", type="primary"):
                decide_flag(f.id, "upheld", reviewer, note)
                st.rerun()
            if c2.button("Dismiss", key=f"fd{f.id}"):
                decide_flag(f.id, "dismissed", reviewer, note)
                st.rerun()
            if c3.button("Reviewed, no action", key=f"fo{f.id}"):
                decide_flag(f.id, "noted", reviewer, note)
                st.rerun()


def page_appeals(reviewer: str):
    st.header("Appeals")
    with db.session() as s:
        appeals = s.scalars(select(Appeal).where(Appeal.kind == "appeal", Appeal.status == "open")
                            .order_by(Appeal.filed_at)).all()
        subs = {x.id: x for x in s.scalars(select(Submission))}
        checks = {c.id: c for c in s.scalars(select(Check).where(Check.result == "hard_fail"))}
        flags = {f.id: f for f in s.scalars(select(Flag).where(Flag.status == "upheld"))}
    if not appeals:
        st.info("No open appeals.")
    for a in appeals:
        sub = subs[a.submission_id]
        with st.expander(f"#{a.id} · R{a.round} · {team_label(sub)} · filed {a.filed_at:%Y-%m-%d %H:%M}"):
            st.write(f"**Decision:** {sub.status_reason}")
            st.write("**Team's appeal:**")
            st.text(a.message or "")
            related = [c for c in checks.values() if c.submission_id == sub.id and (a.check_id in (None, c.id))]
            for c in related:
                st.write(f"Rule `{c.rule}`: {c.summary}")
                st.json(c.proof, expanded=False)
            if a.flag_id and a.flag_id in flags:
                st.write(f"Flag `{flags[a.flag_id].type}`: {flags[a.flag_id].reason}")
                st.json(flags[a.flag_id].evidence, expanded=False)
            note = st.text_input("Decision note (shown to the team)", key=f"an{a.id}")
            c1, c2 = st.columns(2)
            if c1.button("Accept: reinstate team", key=f"aa{a.id}", type="primary"):
                decide_appeal(a.id, True, reviewer, note)
                st.rerun()
            if c2.button("Reject", key=f"ar{a.id}"):
                decide_appeal(a.id, False, reviewer, note)
                st.rerun()


def page_team(_reviewer: str):
    st.header("Team lookup")
    q = st.text_input("Team ID, name or repo")
    if not q:
        return
    with db.session() as s:
        like = f"%{q}%"
        subs = s.scalars(select(Submission).where(Submission.team_id.ilike(like) | Submission.team_name.ilike(like)
                                                  | Submission.repo_url.ilike(like)).limit(20)).all()
        if not subs:
            st.warning("No match.")
            return
        sub = subs[0] if len(subs) == 1 else next(x for x in subs if team_label(x) == st.selectbox(
            "Pick", [team_label(x) for x in subs]))
        st.subheader(team_label(sub))
        st.write({"status": sub.status, "round": sub.round, "reason": sub.status_reason, "track": sub.track,
                  "repo": sub.repo_url, "live": sub.live_url, "frozen_sha": sub.frozen_sha,
                  "frozen_at": sub.frozen_at, "frozen_source": sub.frozen_source})
        st.markdown("**Checks**")
        st.dataframe(pd.DataFrame([(c.round, c.rule, c.result, c.overturned, c.summary) for c in s.scalars(
            select(Check).where(Check.submission_id == sub.id))], columns=["round", "rule", "result", "overturned",
                                                                             "summary"]), hide_index=True)
        st.markdown("**Scores**")
        st.dataframe(pd.DataFrame([(x.component, x.value) for x in s.scalars(
            select(Score).where(Score.submission_id == sub.id))], columns=["component", "value"]), hide_index=True)
        for x in s.scalars(select(Score).where(Score.submission_id == sub.id)):
            with st.expander(f"{x.component} detail"):
                st.json(x.detail)
        st.markdown("**Live test attempts**")
        st.dataframe(pd.DataFrame([(a.target, a.attempt, a.is_retest, a.started_at, a.reachable, a.score, a.error)
                                   for a in s.scalars(select(TestAttempt).where(TestAttempt.submission_id == sub.id))],
                                  columns=["target", "attempt", "retest", "started", "reachable", "score", "error"]),
                     hide_index=True)
        st.dataframe(pd.DataFrame([(r.target, r.attempt, r.test_id, r.passed, r.message) for r in s.scalars(
            select(TestResult).where(TestResult.submission_id == sub.id))],
            columns=["target", "attempt", "test", "passed", "message"]), hide_index=True)
        with st.expander("Request log"):
            st.dataframe(pd.DataFrame([(r.attempt, r.request_id, r.sent_at, r.method, r.url, r.status_code,
                                        r.latency_ms, r.error) for r in s.scalars(
                select(TestRequest).where(TestRequest.submission_id == sub.id).order_by(TestRequest.id))],
                columns=["attempt", "request_id", "time", "method", "url", "status", "latency_ms", "error"]),
                hide_index=True)
        st.markdown("**Flags**")
        st.dataframe(pd.DataFrame([(f.id, f.round, f.type, f.status, f.reviewer, f.reason) for f in s.scalars(
            select(Flag).where(Flag.submission_id == sub.id))],
            columns=["id", "round", "type", "status", "reviewer", "reason"]), hide_index=True)
        llm = s.scalars(select(LLMScore).where(LLMScore.submission_id == sub.id)).all()
        if llm:
            st.markdown("**LLM rubric**")
            for r in llm:
                st.write(f"Pass {r.pass_no}: {r.value} {('error: ' + r.error) if r.error else ''}")
                st.json(r.criteria, expanded=False)
        ui = s.scalars(select(UICheck).where(UICheck.submission_id == sub.id)).first()
        if ui:
            st.markdown("**UI check**")
            st.write({"ok": ui.ok, "status": ui.status_code, "title": ui.title, "load_ms": ui.load_ms,
                      "console_errors": ui.console_errors[:5], "error": ui.error})
            if ui.screenshot_path and os.path.exists(ui.screenshot_path):
                st.image(ui.screenshot_path)
        st.markdown("**Notices**")
        for n in s.scalars(select(Notice).where(Notice.submission_id == sub.id)):
            st.write(f"{n.created_at:%Y-%m-%d %H:%M} · {n.kind} · {n.body_path} · {n.links}")


def page_ranking(_reviewer: str):
    from hackeval.ranking import COMPONENTS, final_ranking

    st.header("R2 ranking")
    st.caption("Computed live from current scores (nothing is written).")
    rows = final_ranking(write=False)
    df = pd.DataFrame([{"rank": r.rank, "team_id": r.team_id, "track": r.track, "total": r.total,
                        **{c: r.comps[c] for c in COMPONENTS}, "open_flags": ", ".join(r.open_flags)}
                       for r in rows])
    st.dataframe(df, hide_index=True, height=600)
    st.download_button("Download CSV", df.to_csv(index=False), "ranking.csv")


def page_audit(reviewer: str):
    st.header("Random audit of automatic rejections")
    rnd = st.selectbox("Round", [1, 2, 3, 4])
    st.write(audit_eval(rnd))
    with db.session() as s:
        rows = s.scalars(select(Audit).where(Audit.round == rnd, Audit.verdict == "pending")).all()
        subs = {x.id: x for x in s.scalars(select(Submission))}
        checks = {c.id: c for c in s.scalars(select(Check))}
    for a in rows:
        sub = subs[a.submission_id]
        with st.expander(f"Audit #{a.id} · {team_label(sub)}"):
            st.write(a.reason)
            if a.check_id:
                st.json(checks[a.check_id].proof, expanded=False)
            note = st.text_input("Note", key=f"au{a.id}")
            c1, c2 = st.columns(2)
            if c1.button("Rejection was correct", key=f"ac{a.id}"):
                audit_record(a.id, True, reviewer, note)
                st.rerun()
            if c2.button("Rejection was WRONG", key=f"aw{a.id}", type="primary"):
                audit_record(a.id, False, reviewer, note)
                st.rerun()
    with db.session() as s:
        st_ = s.get(RoundState, rnd)
    if st_ and st_.paused and st.button(f"Resume round {rnd} (cause fixed)"):
        set_paused(rnd, False, f"resumed by {reviewer}", actor=reviewer)
        st.rerun()


def page_sandbox(reviewer: str):
    st.header("R4 sandbox runs")
    with db.session() as s:
        runs = s.scalars(select(SandboxRun)).all()
        subs = {x.id: x for x in s.scalars(select(Submission))}
    st.dataframe(pd.DataFrame([{"team": subs[r.submission_id].team_id, "strategy": r.strategy,
                                "verdict": r.verdict, "live": r.live_score, "sandbox": r.sandbox_score,
                                "spot_checked_by": r.spot_checked_by} for r in runs]), hide_index=True)
    for r in runs:
        with st.expander(f"{team_label(subs[r.submission_id])} · {r.verdict}"):
            st.json(r.detail, expanded=False)
            if r.log_path and os.path.exists(r.log_path):
                st.code(open(r.log_path, errors="ignore").read()[-6000:])
            note = st.text_input("Spot-check note", key=f"sn{r.id}", value=r.spot_check_note or "")
            if st.button("Record spot-check", key=f"sb{r.id}"):
                with db.session() as s:
                    rr = s.get(SandboxRun, r.id)
                    rr.spot_checked_by, rr.spot_check_note = reviewer, note
                st.rerun()
    st.subheader("Manual build override")
    team = st.text_input("Team ID")
    dockerfile = st.text_area("Dockerfile (optional)")
    subdir = st.text_input("Sub-directory (optional)")
    port = st.number_input("Port", value=cfg.sandbox.default_port)
    if st.button("Save override and re-run") and team:
        from hackeval import jobs

        with db.session() as s:
            sub = s.scalars(select(Submission).where(Submission.team_id == team)).first()
            if sub is None:
                st.error("Unknown team")
                return
            ov = s.get(SandboxOverride, sub.id) or SandboxOverride(submission_id=sub.id)
            ov.dockerfile, ov.subdir, ov.port = dockerfile or None, subdir or None, int(port)
            s.merge(ov)
            jobs.enqueue(s, "sandbox", str(sub.id), {"submission_id": sub.id}, requeue=True)
        st.success("Override saved; sandbox job re-queued.")


def page_judging_results(_reviewer: str):
    st.header("Judging results")
    rnd = st.selectbox("Round", [5, 6])
    res = judge_results(rnd, write=False)
    st.caption("Ranked by mean per-judge z-score (normalised), so strict and generous judges count equally.")
    st.dataframe(pd.DataFrame(res), hide_index=True)
    with db.session() as s:
        judges = s.scalars(select(Judge)).all()
        conflicts = s.scalars(select(JudgeConflict)).all()
    st.subheader("Judges and passcodes")
    st.dataframe(pd.DataFrame([(j.name, j.email, j.passcode) for j in judges],
                              columns=["name", "email", "passcode"]), hide_index=True)
    st.caption(f"{len(conflicts)} declared conflicts.")


def page_judge(judge_id: int):
    with db.session() as s:
        judge = s.get(Judge, judge_id)
        assigned = s.execute(select(JudgeAssignment, Submission).join(
            Submission, Submission.id == JudgeAssignment.submission_id).where(
            JudgeAssignment.judge_id == judge_id).order_by(JudgeAssignment.round, Submission.team_id)).all()
        mine = {(x.submission_id, x.round, x.criterion): x.value for x in s.scalars(
            select(JudgeScore).where(JudgeScore.judge_id == judge_id))}
    st.header(f"Scoring: {judge.name}")
    mx = cfg.judging.scale_max
    if not assigned:
        st.info("No teams assigned yet.")
    for a, sub in assigned:
        crit = criteria_for(a.round)
        done = all((sub.id, a.round, c) in mine for c in crit)
        with st.expander(f"{'✅ ' if done else ''}R{a.round} · {team_label(sub)}"):
            st.write(f"Repo: {sub.repo_url}  \nLive: {sub.live_url}")
            vals = {c: st.slider(f"{c} (weight {w})", 0.0, float(mx), float(mine.get((sub.id, a.round, c), mx / 2)),
                                 0.5, key=f"j{a.id}{c}") for c, w in crit.items()}
            comment = st.text_area("Comment", key=f"jc{a.id}")
            if st.button("Save scores", key=f"js{a.id}"):
                try:
                    record_score(judge_id, sub.id, a.round, vals, comment)
                    st.success("Saved")
                except ValueError as e:
                    st.error(str(e))


who = login()
if who:
    role, ident = who
    st.sidebar.write(f"Signed in: **{ident if role == 'reviewer' else 'judge'}**")
    if st.sidebar.button("Sign out"):
        del st.session_state["who"]
        st.rerun()
    if role == "judge":
        page_judge(ident)
    else:
        pages = {"Dashboard": page_dashboard, "Flag queue": page_flags, "Appeals": page_appeals,
                 "Team lookup": page_team, "R2 ranking": page_ranking, "Audit": page_audit,
                 "Sandbox (R4)": page_sandbox, "Judging results": page_judging_results}
        choice = st.sidebar.radio("Page", list(pages))
        pages[choice](ident)

# HackEval: hackathon evaluation engine

Implements the *Hackathon Evaluation Engine Plan*: it takes ~10,000 submissions
(GitHub repo + live link) to a winner, with every round automated except the
final judging. Website voting (R3) stays on the hackathon website; this engine
exports the shortlist and imports the voted teams back.

| Round | What the engine does | Code |
|---|---|---|
| R0 Freeze | Records each repo's head commit at the deadline (GitHub GraphQL, 100 repos/call, `git ls-remote` fallback), then takes blob-less clones. A second look 24 h later detects rewritten history. | `snapshot.py`, `github_api.py` |
| R1 Rule gates | Private/missing repo, created before start, too few commits → **hard fail** with stored proof. First-commit dump, fork, imported history, size, duplicate repo/live link/code → **flag** for a person. | `rules.py`, `gitstats.py` |
| Evidence + appeals | Every hard fail gets an HTML/JSON evidence report and a 24 h appeal link. No email yet: reports go to `data/outbox/` and `hackeval notices export` lists them for sending. | `notices.py`, `review.py`, `web/public.py` |
| R2 Scoring | Hidden contract tests on the live link (50%), code quality (15%), development authenticity incl. plagiarism (15%), LLM rubric for the top 500 (20%). Weighted ranking, human review of the cut-off band. | `livetest/`, `quality.py`, `plagiarism.py`, `r2.py`, `llm/`, `ranking.py` |
| R3 Voting | CSV export of the top 200; CSV import of the voted 60. | `ranking.py` |
| R4 Deep review | Builds the frozen code in Docker (no internet, CPU/memory limits), runs the hidden tests against it and compares with the live link. | `sandbox.py` |
| R5/R6 Judging | Judge assignment with conflicts of interest, scoring forms, per-judge z-score normalisation. | `judging.py`, `admin/app.py` |
| Fairness | Random 2% audit of automatic rejections; a round pauses if more than 1% are wrong. | `audit.py` |

Everything runs on one server: PostgreSQL, asyncio workers reading a `jobs`
table (every job is safe to re-run), a Streamlit admin page and a small public
FastAPI site for appeals and re-tests.

## Setup

```bash
uv venv -p 3.12 && uv pip install -e ".[admin,llm,dev]"     # or pip install -e ...
cp hackeval.example.yaml hackeval.yaml                     # fill in dates, rules, tester IP, weights
cp deploy/env.example .env                                 # GITHUB_TOKEN, LLM key, admin password
scripts/install_tools.sh                                   # optional: semgrep, gitleaks, scc, JPlag, Playwright, Docker network
hackeval init-db
```

Services on the event server (`deploy/*.service`): `hackeval worker` (all
background jobs), `hackeval serve` (public appeal/re-test pages, put it behind
HTTPS at `event.public_base_url`) and the admin page
(`streamlit run hackeval/admin/app.py`, keep it private, e.g. behind a VPN or
SSH tunnel). `scripts/backup.sh` runs the daily database backup.

## Vicodathon (48-hour vibe-coding hackathon)

Start from `vicodathon.example.yaml`. AI-written code is expected, so:

* **Off:** code-similarity plagiarism, commit-pattern scoring, the first-commit-size flag
  (the same AI tools give similar code and few, large commits to everyone).
* **Still caught:** repo created before the start, the same repo or live link or a fork of
  another team, byte-identical projects, pushes after the deadline, leaked API keys.
* **Score:** hidden API tests 60%, LLM product rubric 30% (`rubric_path: vibe`: problem fit,
  completeness, product thinking), hygiene 10% (no leaked keys, README).
* **Several problem statements:** `ranking.per_track: true` ranks each one on its own and splits
  the voting places and LLM places between them by number of teams. A reviewer can move a
  team that picked the wrong problem statement (Team lookup → Change problem statement);
  its old results are cleared and it is re-tested.

## Problem statements (tracks)

Each problem statement is a folder under `tracks_dir` (see `tracks/sample_todo`):

```
track.yaml          name, problem statement (also given to the LLM), warm-up path
public_tests.py     sample tests published to participants
hidden_tests.py     hidden contract tests (keep these out of any public repo)
starter/            optional starter code (ignored by plagiarism checks)
rubric.yaml         optional per-track LLM rubric
```

Tests are plain async functions using `hackeval.livetest.framework` (httpx
only). The same file is shipped to participants, so the self-check runs the
exact code the engine uses:

```python
@contract_test(category="crud", weight=2)
async def create(ctx):
    r = await ctx.post("/todos", json={"title": "x"})
    ctx.expect_status(r, 201)
    ctx.state["id"] = ctx.expect_json(r)["id"]     # later tests can use it
```

The `track` column of the submission CSV picks the track. Try a suite with
`hackeval selfcheck --track sample_todo https://your-app` (`--hidden` for the
hidden suite).

## Before the event

1. Decide the open items from the plan and put them in `hackeval.yaml`:
   minimum commit count, weights, voting numbers, track quotas.
2. `hackeval public-kit kit/` builds what you publish: `rulecheck.py`
   (participants run it on their own repo), sample tests per track with the
   test runner, the problem statements, `published_rules.json` (dates, rules,
   weights, tester IP / User-Agent / request-ID header) and the rubric.
3. **Dry run on the last hackathon** (see below) and **LLM calibration** on 100
   hand-scored entries:
   `hackeval llm import-hand-scores hand.csv && hackeval llm calibrate`
   (writes `data/reports/llm_calibration.json`: Pearson/Spearman, error, bias per criterion).

### Dry run on a past hackathon

Set `event.replay: true` with the old event's `start`/`deadline` in a separate
config and database. In replay mode the frozen commit is the last commit on the
default branch before the old deadline. Run the pipeline as below (use
`llm.provider: mock` for a free run), then compare with what actually happened:

```bash
hackeval replay-compare past_outcomes.csv    # team_id, past_status, past_rank
```

The report (`data/reports/replay_comparison.json`) lists teams the engine would
reject that were not rejected then (and the reverse), the rank correlation, and
past finalists that would fall below the cut-off. Old live links are probably
down, so the test component will mostly be zero in a replay.

## Event runbook

`hackeval status` shows teams per round, open flags/appeals, paused rounds and the job queue at any time.

| When | Command |
|---|---|
| Before deadline | `hackeval import-submissions website_export.csv` (re-import with `--update` until the deadline) |
| **Deadline** | `hackeval freeze` (minutes; records head commits), workers clone everything (2-3 h) and run the R1 rules automatically |
| Clones done | `hackeval r1 finalize` (cross-team checks; hard fails disqualified with evidence; the rest advance) → `hackeval notices export r1.csv` → send the CSV's links to teams |
| Day 1 | `hackeval r2 start` (full code → quality + fingerprints; live tests at 0 h, 3 h, 6 h). Reviewers work the flag queue and appeals in the admin page. `hackeval audit sample --round 1` |
| Day 1 evening | `hackeval notices export r2.csv` (test logs + re-test links for dead links) |
| Day 2 | `hackeval r2 plagiarism`, `hackeval r2 authenticity`, `hackeval r2 llm-pass1`, `hackeval r2 llm-poll --wait`, `hackeval r2 llm-pass2`, `hackeval r2 llm-poll --wait`, optional `hackeval r2 ui-checks`, then `hackeval r2 rank --csv ranking.csv` (flags the cut-off band for review) |
| Day 3 | Clear cut-off flags, then `hackeval voting export top200.csv --finalize` → upload to the website. `hackeval audit sample --round 2` |
| Day 5 | `hackeval voting import voted60.csv`, `hackeval r4 start`, spot-check runs in the admin page (add a build override where auto-detection fails), `hackeval r4 advance --max 50` |
| Days 6-7 | `hackeval judges import judges.csv`, `hackeval judges assign --round 5`; judges score in the admin page; `hackeval judges advance --round 5 --keep 10` |
| Finale | `hackeval judges assign --round 6`, judges score the live demo, `hackeval judges results --round 6` |

Rounds paused by the audit refuse to advance until `hackeval audit resume --round N`.

## How decisions are made

* **Hard fails** use only facts participants cannot edit: GitHub's `createdAt`
  and visibility, and our own deadline snapshot. Git dates are never used for a
  hard fail.
* **Flags** never disqualify on their own. A reviewer upholds (disqualify,
  with an evidence report and appeal link), dismisses, or notes them.
* **Live tests**: one published User-Agent and IP, a unique `X-Eval-Request-ID`
  on every request, each request stored with time, URL, status, latency and
  body. The first request of each attempt waits up to 90 s. The best of three
  attempts counts. A dead link scores zero for that component only, and the
  team gets the full request log plus one re-test. Requests are throttled per
  host and per hosting platform (`*.vercel.app` etc. share a limit).
* **Plagiarism**: normalised token fingerprints (renaming variables does not
  hide a copy), boilerplate shared by many repos removed, MinHash LSH for
  candidates, exact containment, then JPlag on candidate pairs when configured.
  Pairs above the threshold go to the human queue.
* **LLM rubric**: fixed rubric with level descriptions and examples
  (`hackeval/rubric/default.yaml`); README, file tree and key files up to a
  token cap; repo text is passed as untrusted data (closing tags neutralised,
  injection attempts flagged for review); top 500 only; teams near the cut-off
  scored twice and averaged. Providers: `anthropic` (Message Batches API,
  default model `claude-haiku-5-5`), `openai` (Batch API) or `mock` (offline).
* **Judging**: each judge's totals are converted to z-scores across the teams
  they scored, so a strict judge cannot sink a team. Conflicted judges are
  never assigned and cannot submit scores.
* Every check and score stores the engine version that produced it.

## Not included yet

* **Email**: evidence reports and links are exported as CSV for sending
  (`hackeval notices export`). A mail backend only needs to read undelivered
  `notices` rows.
* **Voting** itself (on the website).
* Sandbox builds for repos without a Dockerfile, `package.json` start script
  or a recognisable Python entry point are flagged `manual`; add a Dockerfile
  override in the admin page.

## Tests

```bash
pytest                                     # SQLite, fully offline (~15 s)
HACKEVAL_TEST_DATABASE_URL=postgresql+psycopg://user@host/db pytest   # same suite on PostgreSQL
ruff check hackeval tests public
```

`tests/test_pipeline.py` runs freeze → R1 → R2 (live tests against in-process
apps, plagiarism, mock LLM) → voting hand-off → R4 → judging end to end.

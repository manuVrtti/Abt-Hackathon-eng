"""Freeze -> R1 -> R2 -> voting hand-off -> R4 -> judging, all offline."""

from __future__ import annotations

import csv

import httpx
from sqlalchemy import select

from hackeval import db, jobs
from hackeval.judging import advance, assign, import_judges, record_score, results
from hackeval.livetest import runner
from hackeval.llm.scoring import poll_batches, run_pass1
from hackeval.models import (
    Appeal, Judge, JudgeAssignment, LLMScore, Notice, PlagiarismPair, SandboxRun, Score, Submission, TestAttempt,
    TestRequest,
)
from hackeval.plagiarism import run_plagiarism
from hackeval.r2 import compute_authenticity, start_r2
from hackeval.ranking import export_voting, final_ranking, import_voted
from hackeval.review import redeem_retest
from hackeval.rules import apply_r1_decisions, cross_team_checks
from hackeval.sandbox import advance_r4
from hackeval.snapshot import freeze_heads

from .conftest import code_file, make_repo
from .todo_app import make_app


class HostRouter(httpx.AsyncBaseTransport):
    """Route requests to in-process ASGI apps by hostname."""

    def __init__(self, apps: dict[str, object]):
        self.t = {h: httpx.ASGITransport(app=a) for h, a in apps.items()}

    async def handle_async_request(self, request):
        t = self.t.get(request.url.host)
        if t is None:
            raise httpx.ConnectError("unreachable", request=request)
        return await t.handle_async_request(request)


DISTINCT = """
class Inventory:
    def __init__(self, store, limit=10):
        self.store = store
        self.items = {}
        self.limit = limit

    def add(self, sku, qty):
        if qty <= 0 or len(self.items) >= self.limit:
            raise ValueError("bad quantity")
        self.items[sku] = self.items.get(sku, 0) + qty
        return self.items[sku]

    def remove(self, sku, qty):
        have = self.items.get(sku, 0)
        while qty > 0 and have > 0:
            have -= 1
            qty -= 1
        self.items[sku] = have
        return [k for k, v in self.items.items() if v == 0]

    def report(self, fmt="csv"):
        rows = sorted(self.items.items(), key=lambda kv: (-kv[1], kv[0]))
        out = []
        for sku, qty in rows:
            out.append(f"{sku},{qty}" if fmt == "csv" else {"sku": sku, "qty": qty})
        return out
"""


def distinct(n: int) -> str:
    return "\n".join(DISTINCT.replace("Inventory", f"Inventory{i}") for i in range(n))


def renamed(src: str) -> str:
    for a, b in [("handler_", "fn_"), ("total", "acc"), ("value", "x"), ("Inventory", "Stock"), ("items", "things"),
                 ("sku", "code"), ("qty", "amount")]:
        src = src.replace(a, b)
    return src


def test_full_pipeline(settings, tmp_path, monkeypatch):
    settings.tester.spread_hours = 0
    settings.ranking.to_voting = 2
    settings.ranking.cutoff_review_band = 1
    settings.plagiarism.boilerplate_df = 0.9

    base = [{"app.py": distinct(6), "README.md": "# Todo\n## Setup\nInstall and run. Built with FastAPI.\n"}]
    good = make_repo(tmp_path / "good", base + [{f"m{i}.py": code_file(10 + i)} for i in range(6)])
    copier = make_repo(tmp_path / "copier", [{"main.py": renamed(distinct(6))}] +
                       [{f"x{i}.py": renamed(code_file(10 + i))} for i in range(6)])
    broken = make_repo(tmp_path / "broken", [{f"b{i}.py": code_file(50 + i)} for i in range(6)])
    plain = make_repo(tmp_path / "plain", [{f"p{i}.py": code_file(80 + i)} for i in range(7)])
    seen: list[dict] = []
    monkeypatch.setattr(runner, "TRANSPORT", HostRouter({
        "good.test": make_app(seen_headers=seen), "copier.test": make_app(), "plain.test": make_app(),
        "broken.test": make_app(broken=True)}))

    with db.session() as s:
        for name, repo in [("good", good), ("copier", copier), ("broken", broken), ("plain", plain)]:
            s.add(Submission(team_id=name, team_name=name, repo_url=str(repo), live_url=f"https://{name}.test",
                             track="sample_todo"))
    freeze_heads()
    jobs.drain()
    cross_team_checks()
    assert apply_r1_decisions()["advanced"] == 4

    assert start_r2() == 4
    jobs.drain()
    with db.session() as s:
        ids = {x.team_id: x.id for x in s.scalars(select(Submission))}
        scores = {(sc.submission_id, sc.component): sc.value for sc in s.scalars(select(Score))}
        assert scores[(ids["good"], "tests")] == 100
        assert scores[(ids["broken"], "tests")] == 0
        assert len(s.scalars(select(TestAttempt).where(TestAttempt.submission_id == ids["broken"])).all()) == 3
        reqs = s.scalars(select(TestRequest).where(TestRequest.submission_id == ids["good"])).all()
        assert reqs[0].test_id == "_warmup" and all(r.request_id for r in reqs)
        assert all(r.status_code is not None and r.latency_ms is not None for r in reqs)
        assert all((ids["good"], c) in scores for c in ("quality",))
        notice = s.scalars(select(Notice).where(Notice.submission_id == ids["broken"])).one()
        assert notice.kind == "r2-testlog" and "retest" in notice.links
        retest_token = s.scalars(select(Appeal).where(Appeal.submission_id == ids["broken"],
                                                      Appeal.kind == "retest")).one().token
    assert any(h.get("x-eval-request-id") for h in seen)
    assert all(h.get("user-agent", "").startswith("HackEval-Tester") for h in seen)

    # The broken team fixes its server and uses its one re-test.
    runner.TRANSPORT.t["broken.test"] = httpx.ASGITransport(app=make_app())
    redeem_retest(retest_token)
    try:
        redeem_retest(retest_token)
        raise AssertionError("second re-test must be refused")
    except ValueError:
        pass
    jobs.drain()
    with db.session() as s:
        assert s.scalars(select(Score).where(Score.submission_id == ids["broken"],
                                             Score.component == "tests")).one().value == 100

    plag = run_plagiarism()
    assert plag["flagged_pairs"] >= 1
    with db.session() as s:
        pair = s.scalars(select(PlagiarismPair)).first()
        assert {pair.a_id, pair.b_id} == {ids["good"], ids["copier"]}
    assert compute_authenticity() == 4

    assert run_pass1()["submitted"] == 4
    assert poll_batches()["ingested"] == 1
    with db.session() as s:
        assert all(r.value is not None for r in s.scalars(select(LLMScore)))

    rows = final_ranking()
    assert [r.rank for r in rows] == [1, 2, 3, 4]
    copier_row = next(r for r in rows if r.team_id == "copier")
    assert "plagiarism" in copier_row.open_flags

    out = tmp_path / "top.csv"
    assert export_voting(out, finalize=True) == 2
    top = [r["team_id"] for r in csv.DictReader(open(out))]
    voted = tmp_path / "voted.csv"
    voted.write_text("team_id,votes\n" + f"{top[0]},120\n")
    assert import_voted(voted) == {"advanced": 1, "eliminated": 1, "unknown": 0}

    with db.session() as s:
        winner = s.scalars(select(Submission).where(Submission.round == 4)).one()
        s.add(SandboxRun(submission_id=winner.id, verdict="consistent", build_ok=True, run_ok=True))
    assert advance_r4(max_teams=50)["advanced"] == 1

    judges = tmp_path / "judges.csv"
    judges.write_text("name,email,conflicts\nAsha,a@x,\nRavi,r@x,\nMeena,m@x,\nKiran,k@x," + top[0] + "\n")
    assert import_judges(judges) == 4
    res = assign(5)
    assert res["understaffed"] == []
    with db.session() as s:
        assigned = s.scalars(select(JudgeAssignment)).all()
        kiran = s.scalars(select(Judge).where(Judge.name == "Kiran")).one()
        assert kiran.id not in {a.judge_id for a in assigned}  # conflict respected
    for a in assigned:
        record_score(a.judge_id, a.submission_id, 5, {k: 7 for k in settings.judging.criteria})
    assert results(5)[0]["team_id"] == top[0]
    assert advance(5, keep=1)["kept"] == 1

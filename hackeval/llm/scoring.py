"""Run the LLM rubric over the top teams.

Pass 1: the top ``llm.top_n`` of the preliminary ranking (split between
tracks when ranking per track), one score each.
Pass 2: teams within ``llm.second_pass_band`` places of the voting cut-off
are scored a second time and the two scores are averaged.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from .. import db
from ..config import get_settings
from ..models import LLMBatch, LLMScore, Submission
from ..quality import set_score
from ..review import raise_flag
from ..snapshot import code_path
from ..tracks import get_track
from .base import LLMRequest, Provider, get_provider
from .packer import pack_repo
from .rubric import ParseError, Rubric, load_rubric, parse_response, system_prompt, user_prompt

log = logging.getLogger("hackeval.llm")


def rubric_for(track_id: str | None = None) -> Rubric:
    cfg = get_settings()
    if track_id:
        try:
            t = get_track(track_id)
            if t.rubric_path:
                return load_rubric(t.rubric_path)
        except KeyError:
            pass
    return load_rubric(cfg.llm.rubric_path)


def provider(rubric: Rubric) -> Provider:
    c = get_settings().llm
    return get_provider(c.provider, c.model, c.max_output_tokens, rubric.schema())


def custom_id(sub_id: int, pass_no: int) -> str:
    return f"s{sub_id}-p{pass_no}"


def build_request(sub_id: int, pass_no: int) -> tuple[LLMRequest, dict] | None:
    cfg = get_settings()
    with db.session() as s:
        sub = s.get(Submission, sub_id)
        root = code_path(s, sub_id)
        track_id = sub.track
    if root is None:
        return None
    try:
        track = get_track(track_id)
        tname, statement = track.name, track.problem_statement
    except KeyError:
        tname, statement = track_id, "(no problem statement configured for this track)"
    rubric = rubric_for(track_id)
    packed, meta = pack_repo(root, cfg.llm.token_cap)
    return LLMRequest(custom_id(sub_id, pass_no), system_prompt(rubric, cfg.event.name),
                      user_prompt(tname, statement, packed)), meta


def ingest(sub_id: int, pass_no: int, text: str | None, error: str | None, tokens_est: int | None = None) -> None:
    cfg = get_settings()
    with db.session() as s:
        sub = s.get(Submission, sub_id)
        rubric = rubric_for(sub.track)
        row = s.scalars(select(LLMScore).where(LLMScore.submission_id == sub_id, LLMScore.pass_no == pass_no)).first()
        if row is None:
            row = LLMScore(submission_id=sub_id, pass_no=pass_no)
            s.add(row)
        row.provider, row.model, row.raw, row.error = cfg.llm.provider, cfg.llm.model, text, error
        row.input_tokens_est = tokens_est
        row.value, row.criteria = None, {}
        if error is None:
            try:
                scores, reasons, inj = parse_response(rubric, text)
                row.criteria = {k: {"score": v, "reason": reasons[k]} for k, v in scores.items()}
                row.value = rubric.value(scores)
                row.injection_suspected = inj
                if inj:
                    raise_flag(s, sub_id, 2, "prompt_injection",
                               "The LLM judge reported text in the repository that tries to influence grading.",
                               {"pass": pass_no})
            except ParseError as e:
                row.error = f"parse: {e}"
        s.flush()
        _update_llm_component(s, sub_id)


def _update_llm_component(s, sub_id: int) -> None:
    rows = s.scalars(select(LLMScore).where(LLMScore.submission_id == sub_id, LLMScore.value.is_not(None))).all()
    if not rows:
        return
    value = round(sum(r.value for r in rows) / len(rows), 2)
    set_score(s, sub_id, "llm", value, {"passes": {r.pass_no: r.value for r in rows},
                                        "criteria": {r.pass_no: r.criteria for r in rows}})


def score_submissions(sub_ids: list[int], pass_no: int, batch: bool | None = None, workers: int = 8) -> dict:
    cfg = get_settings()
    batch = cfg.llm.batch if batch is None else batch
    built = {}
    for sid in sub_ids:
        b = build_request(sid, pass_no)
        if b is None:
            ingest(sid, pass_no, None, "no code snapshot")
        else:
            built[sid] = b
    if not built:
        return {"submitted": 0}
    # Tracks may have their own rubric (and so their own output schema).
    with db.session() as s:
        tracks = {sid: s.get(Submission, sid).track for sid in built}
    groups: dict[str, list[int]] = {}
    for sid, t in tracks.items():
        groups.setdefault(t, []).append(sid)
    out: dict = {"submitted": 0, "scored": 0, "batches": []}
    for track_id, sids in groups.items():
        prov = provider(rubric_for(track_id))
        if batch:
            batch_id = prov.submit_batch([built[sid][0] for sid in sids])
            with db.session() as s:
                s.add(LLMBatch(provider=prov.name, external_id=batch_id, pass_no=pass_no, submission_ids=sids))
            out["submitted"] += len(sids)
            out["batches"].append(batch_id)
            continue

        def one(sid, prov=prov):
            req, meta = built[sid]
            res = prov.complete(req)
            ingest(sid, pass_no, res.text, res.error, meta["tokens_est"])

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, sids))
        out["scored"] += len(sids)
    return out


def poll_batches() -> dict:
    out = {"pending": 0, "ingested": 0}
    with db.session() as s:
        batches = s.scalars(select(LLMBatch).where(LLMBatch.status == "submitted")).all()
        s.expunge_all()
    for b in batches:
        prov = provider(rubric_for(None))
        if not prov.batch_done(b.external_id):
            out["pending"] += 1
            continue
        seen = set()
        for res in prov.batch_results(b.external_id):
            sid = int(res.custom_id.split("-")[0][1:])
            ingest(sid, b.pass_no, res.text, res.error)
            seen.add(sid)
        for sid in set(b.submission_ids) - seen:
            ingest(sid, b.pass_no, None, "missing from batch results")
        with db.session() as s:
            s.get(LLMBatch, b.id).status = "ingested"
        out["ingested"] += 1
    return out


def run_pass1() -> dict:
    from ..ranking import llm_pool, preliminary

    rows = preliminary()
    pool = llm_pool(rows)
    top = [r.sub_id for r in rows if r.sub_id in pool]
    with db.session() as s:
        done = {r.submission_id for r in s.scalars(select(LLMScore).where(LLMScore.pass_no == 1,
                                                                         LLMScore.value.is_not(None)))}
    return score_submissions([i for i in top if i not in done], 1)


def run_pass2() -> dict:
    from ..ranking import final_ranking, near_cutoff

    cfg = get_settings()
    ids = [r.sub_id for r in near_cutoff(final_ranking(write=False), cfg.llm.second_pass_band)]
    with db.session() as s:
        done = {r.submission_id for r in s.scalars(select(LLMScore).where(LLMScore.pass_no == 2,
                                                                         LLMScore.value.is_not(None)))}
    return score_submissions([i for i in ids if i not in done], 2)


def retry_failed(pass_no: int = 1) -> dict:
    with db.session() as s:
        ids = [r.submission_id for r in s.scalars(select(LLMScore).where(LLMScore.pass_no == pass_no,
                                                                        LLMScore.value.is_(None)))]
    return score_submissions(ids, pass_no, batch=False)

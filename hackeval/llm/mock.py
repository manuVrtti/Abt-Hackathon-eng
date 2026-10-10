"""Deterministic offline provider for tests and dry runs (no API calls, no cost).

Scores come from simple text features of the packed repo, so dry runs
produce stable, plausible rankings."""

from __future__ import annotations

import json
import re
import uuid

from .base import LLMRequest, LLMResult, Provider


class MockProvider(Provider):
    name = "mock"

    def complete(self, req: LLMRequest) -> LLMResult:
        repo = req.user.split("<repository>", 1)[-1]
        words = len(re.findall(r"\w+", repo))
        files = repo.count("<file path=")
        fit = 1 + min(4, words // 2000)
        orig = 1 + min(4, files // 3)
        m = re.search(r"^Criteria keys: (.+)$", req.system, re.M)
        names = [n.strip() for n in m.group(1).split(",")] if m else ["problem_fit", "originality"]
        out = {n: {"score": fit if i % 2 == 0 else orig, "reason": "mock score"} for i, n in enumerate(names)}
        out["injection_attempt"] = bool(re.search(r"ignore (all|previous) instructions|full marks", repo, re.I))
        return LLMResult(req.custom_id, json.dumps(out))

    @staticmethod
    def _file(batch_id: str):
        from ..config import get_settings

        return get_settings().path("llm", "mock_batches") / f"{batch_id}.json"

    def submit_batch(self, reqs: list[LLMRequest]) -> str:
        bid = "mock-" + uuid.uuid4().hex[:12]
        self._file(bid).write_text(json.dumps([r.__dict__ for r in reqs]))
        return bid

    def batch_done(self, batch_id: str) -> bool:
        return True

    def batch_results(self, batch_id: str) -> list[LLMResult]:
        reqs = [LLMRequest(**r) for r in json.loads(self._file(batch_id).read_text())]
        return [self.complete(r) for r in reqs]

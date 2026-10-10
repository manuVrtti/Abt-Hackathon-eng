"""OpenAI chat completions; batch mode uses the OpenAI Batch API.
Reads OPENAI_API_KEY from the environment. Set llm.model to the model you want."""

from __future__ import annotations

import io
import json

from .base import LLMRequest, LLMResult, Provider


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, model: str, max_output_tokens: int = 2048, schema: dict | None = None):
        super().__init__(model, max_output_tokens, schema)
        import openai

        self._openai = openai
        self.client = openai.OpenAI()

    def _body(self, req: LLMRequest) -> dict:
        body = {
            "model": self.model,
            "max_completion_tokens": self.max_output_tokens,
            "messages": [{"role": "system", "content": req.system}, {"role": "user", "content": req.user}],
        }
        if self.schema:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "rubric_score", "schema": self.schema}}
        return body

    def complete(self, req: LLMRequest) -> LLMResult:
        try:
            r = self.client.chat.completions.create(**self._body(req))
        except self._openai.APIError as e:
            return LLMResult(req.custom_id, None, f"api_error: {e}")
        choice = r.choices[0]
        if getattr(choice.message, "refusal", None):
            return LLMResult(req.custom_id, None, "refusal")
        return LLMResult(req.custom_id, choice.message.content,
                         "max_tokens" if choice.finish_reason == "length" else None)

    def submit_batch(self, reqs: list[LLMRequest]) -> str:
        lines = [json.dumps({"custom_id": r.custom_id, "method": "POST", "url": "/v1/chat/completions",
                             "body": self._body(r)}) for r in reqs]
        f = self.client.files.create(file=("rubric_batch.jsonl", io.BytesIO("\n".join(lines).encode())),
                                     purpose="batch")
        b = self.client.batches.create(input_file_id=f.id, endpoint="/v1/chat/completions",
                                       completion_window="24h")
        return b.id

    def batch_done(self, batch_id: str) -> bool:
        return self.client.batches.retrieve(batch_id).status in ("completed", "failed", "expired", "cancelled")

    def batch_results(self, batch_id: str) -> list[LLMResult]:
        b = self.client.batches.retrieve(batch_id)
        out = []
        if b.output_file_id:
            for line in self.client.files.content(b.output_file_id).text.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                resp = row.get("response") or {}
                if resp.get("status_code") == 200:
                    choice = resp["body"]["choices"][0]
                    out.append(LLMResult(row["custom_id"], choice["message"].get("content"),
                                         "max_tokens" if choice.get("finish_reason") == "length" else None))
                else:
                    out.append(LLMResult(row["custom_id"], None, f"status {resp.get('status_code')}: {row.get('error')}"))
        if b.error_file_id:
            for line in self.client.files.content(b.error_file_id).text.splitlines():
                if line.strip():
                    row = json.loads(line)
                    out.append(LLMResult(row["custom_id"], None, f"error: {row.get('error')}"))
        return out

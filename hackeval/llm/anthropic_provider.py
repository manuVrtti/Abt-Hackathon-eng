"""Claude via the Anthropic SDK. Batch mode uses the Message Batches API
(asynchronous, half price). Reads ANTHROPIC_API_KEY from the environment."""

from __future__ import annotations

from .base import LLMRequest, LLMResult, Provider


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str, max_output_tokens: int = 2048, schema: dict | None = None):
        super().__init__(model, max_output_tokens, schema)
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic()

    def _params(self, req: LLMRequest) -> dict:
        output_config: dict = {"effort": "low"}
        if self.schema:
            output_config["format"] = {"type": "json_schema", "schema": self.schema}
        return {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "system": req.system,
            "messages": [{"role": "user", "content": req.user}],
            "output_config": output_config,
        }

    @staticmethod
    def _text(msg) -> tuple[str | None, str | None]:
        if msg.stop_reason == "refusal":
            return None, "refusal"
        text = "".join(b.text for b in msg.content if b.type == "text")
        if msg.stop_reason == "max_tokens":
            return text or None, "max_tokens"
        return text, None

    def complete(self, req: LLMRequest) -> LLMResult:
        a = self._anthropic
        try:
            msg = self.client.messages.create(**self._params(req))
        except a.BadRequestError as e:
            return LLMResult(req.custom_id, None, f"bad_request: {e}")
        except a.RateLimitError as e:
            return LLMResult(req.custom_id, None, f"rate_limited: {e}")
        except a.APIStatusError as e:
            return LLMResult(req.custom_id, None, f"api_error {e.status_code}: {e}")
        except a.APIConnectionError as e:
            return LLMResult(req.custom_id, None, f"connection_error: {e}")
        text, err = self._text(msg)
        return LLMResult(req.custom_id, text, err)

    def submit_batch(self, reqs: list[LLMRequest]) -> str:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        batch = self.client.messages.batches.create(requests=[
            Request(custom_id=r.custom_id, params=MessageCreateParamsNonStreaming(**self._params(r))) for r in reqs
        ])
        return batch.id

    def batch_done(self, batch_id: str) -> bool:
        return self.client.messages.batches.retrieve(batch_id).processing_status == "ended"

    def batch_results(self, batch_id: str) -> list[LLMResult]:
        out = []
        for res in self.client.messages.batches.results(batch_id):
            kind = res.result.type
            if kind == "succeeded":
                text, err = self._text(res.result.message)
                out.append(LLMResult(res.custom_id, text, err))
            elif kind == "errored":
                out.append(LLMResult(res.custom_id, None, f"errored: {res.result.error.type}"))
            else:
                out.append(LLMResult(res.custom_id, None, kind))  # canceled / expired
        return out

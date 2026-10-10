"""Provider-neutral LLM interface used by the rubric scorer."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMRequest:
    custom_id: str
    system: str
    user: str


@dataclass
class LLMResult:
    custom_id: str
    text: str | None
    error: str | None = None


class Provider(ABC):
    name: str = "base"

    def __init__(self, model: str, max_output_tokens: int = 2048, schema: dict | None = None):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.schema = schema

    @abstractmethod
    def complete(self, req: LLMRequest) -> LLMResult:
        """One synchronous call (calibration, small runs, retries)."""

    @abstractmethod
    def submit_batch(self, reqs: list[LLMRequest]) -> str:
        """Submit a batch and return the provider's batch ID."""

    @abstractmethod
    def batch_done(self, batch_id: str) -> bool:
        ...

    @abstractmethod
    def batch_results(self, batch_id: str) -> list[LLMResult]:
        ...


def get_provider(name: str, model: str, max_output_tokens: int, schema: dict | None) -> Provider:
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(model, max_output_tokens, schema)
    if name == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(model, max_output_tokens, schema)
    if name == "mock":
        from .mock import MockProvider

        return MockProvider(model, max_output_tokens, schema)
    raise ValueError(f"unknown LLM provider '{name}' (anthropic, openai, mock)")

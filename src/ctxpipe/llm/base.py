"""LLM provider interface used by the compaction engine."""
from __future__ import annotations

from typing import Protocol


class LLM(Protocol):
    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str: ...


def get_llm(provider: str = "anthropic") -> LLM:
    if provider == "anthropic":
        from .anthropic_llm import AnthropicLLM

        return AnthropicLLM()
    raise ValueError(f"unknown LLM provider: {provider}")

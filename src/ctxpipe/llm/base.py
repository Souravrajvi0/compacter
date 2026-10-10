"""LLM provider interface used by the compaction engine."""
from __future__ import annotations

from typing import Protocol


class LLM(Protocol):
    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str: ...


# Which agent CLI can summarize for which agent (reuses that agent's existing login: no API key).
AGENT_CLI = {"claude-code": "claude", "codex": "codex", "opencode": "opencode"}


def get_llm(provider: str = "auto", agent: str | None = None) -> LLM:
    """provider: auto | claude | codex | opencode | anthropic.

    auto: the agent you are working in, else any installed agent CLI, else the Anthropic API.
    """
    if provider == "auto":
        from . import cli_llm
        from .key_pool import keys_from_env

        preferred = AGENT_CLI.get(agent or "")
        order = [c for c in [preferred, "claude", "codex", "opencode"] if c]
        for kind in dict.fromkeys(order):
            if cli_llm.available(kind):
                return cli_llm.AgentLLM(kind)
        if keys_from_env("ANTHROPIC"):
            provider = "anthropic"
        else:
            raise RuntimeError("no summarizer available: install claude/codex/opencode, or set ANTHROPIC_API_KEY")
    if provider in ("claude", "codex", "opencode"):
        from .cli_llm import AgentLLM

        return AgentLLM(provider)
    if provider == "anthropic":
        from .anthropic_llm import AnthropicLLM

        return AnthropicLLM()
    raise ValueError(f"unknown LLM provider: {provider}")

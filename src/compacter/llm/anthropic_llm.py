"""Anthropic-backed LLM with multi-key rotation.

Requires `pip install compacter[anthropic]` and one or more keys in the environment
(see key_pool.keys_from_env).
"""
from __future__ import annotations

import os

from .key_pool import KeyPool, keys_from_env

DEFAULT_MODEL = "claude-sonnet-5-5"
RATE_LIMIT_COOLDOWN = 60.0
BAD_KEY_COOLDOWN = 3600.0
OVERLOAD_COOLDOWN = 15.0


class AnthropicLLM:
    def __init__(self, model: str | None = None, keys: list[str] | None = None) -> None:
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("install the extra: pip install 'compacter[anthropic]'") from e
        self._anthropic = anthropic
        keys = keys or keys_from_env("ANTHROPIC")
        if not keys:
            raise RuntimeError("no API keys: set ANTHROPIC_API_KEY or COMPACTER_ANTHROPIC_KEYS")
        self._pool = KeyPool(keys)
        self._clients: dict[str, object] = {}
        self._model = model or os.environ.get("COMPACTER_MODEL", DEFAULT_MODEL)

    def _classify(self, e: Exception) -> float | None:
        a = self._anthropic
        if isinstance(e, a.RateLimitError):
            return RATE_LIMIT_COOLDOWN
        if isinstance(e, (a.AuthenticationError, a.PermissionDeniedError)):
            return BAD_KEY_COOLDOWN
        if isinstance(e, a.APIStatusError) and e.status_code in (500, 502, 503, 529):
            return OVERLOAD_COOLDOWN
        return None

    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str:
        def call(key: str) -> str:
            client = self._clients.setdefault(key, self._anthropic.Anthropic(api_key=key))
            resp = client.messages.create(
                model=self._model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in resp.content if b.type == "text")

        return self._pool.call(call, self._classify)

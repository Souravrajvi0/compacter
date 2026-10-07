"""Token counting via tiktoken (cl100k_base), heuristic fallback if unavailable."""
from __future__ import annotations

from .models import Message

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - tiktoken missing or broken
    _ENC = None


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENC is None:
        return max(1, len(text) // 4)
    return max(1, len(_ENC.encode(text, disallowed_special=())))


def session_tokens(messages: list[Message]) -> int:
    return sum(estimate_tokens(m.content) for m in messages)

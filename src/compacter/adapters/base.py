"""Adapter interface + registry. One adapter per agent; core never imports agent specifics."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..core.models import Session

_REGISTRY: dict[str, type[Adapter]] = {}


class Adapter(ABC):
    name: str

    @abstractmethod
    def read(self, project: Path) -> Session:
        """Import the agent's current session for `project` into the common format."""

    @abstractmethod
    def write(self, session: Session, project: Path) -> Path:
        """Write a (compacted) session back in the agent's native format.

        Must never destroy the original; return the path of what was written.
        """


def register(cls: type[Adapter]) -> type[Adapter]:
    _REGISTRY[cls.name] = cls
    return cls


def get_adapter(name: str) -> Adapter:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(f"unknown agent {name!r}; available: {sorted(_REGISTRY)}") from None


def resume_command(agent: str, session_id: str | None, written: Path) -> str:
    """How to continue on a session an adapter just wrote."""
    if agent == "claude-code":
        return f"claude --resume {session_id or written.stem}"
    if agent == "codex":
        return "codex resume --last"
    if agent == "opencode" and session_id:
        return f"opencode --session {session_id}"
    return str(written)

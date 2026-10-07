from .base import Adapter, get_adapter, register

# Import adapters for their @register side effect.
from . import aider, claude_code, codex, generic_api  # noqa: E402,F401

__all__ = ["Adapter", "get_adapter", "register"]

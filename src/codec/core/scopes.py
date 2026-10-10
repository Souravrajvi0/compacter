"""Scoped policy rules: matching + specificity/strictness resolution."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any

from .models import Rule


@dataclass
class ScopeRule:
    match: str            # "file:*.log" | "tag:experiment" | "role:user" | "tool_result" | "default"
    rule: Rule


def specificity(match: str) -> int:
    if match.startswith("file:"):
        return 4
    if match.startswith("tag:"):
        return 3
    if match.startswith("role:"):
        return 2
    if match == "tool_result":
        return 1
    return 0


def scope_matches(match: str, msg: Any, msg_path: "str | None") -> bool:
    if match in ("default", "*"):
        return True
    if match == "tool_result":
        return msg.role == "tool"
    if match.startswith("role:"):
        return msg.role == match[5:]
    if match.startswith("tag:"):
        return match[4:] in msg.tags
    if match.startswith("file:"):
        return bool(msg_path) and fnmatch.fnmatchcase(msg_path, match[5:])
    return False

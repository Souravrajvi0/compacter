"""Heuristic auto-tagging of messages."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Message, Session

# (pattern, tag, confidence)
PATTERNS: list[tuple[str, str, float]] = [
    (r"\bgoal:|\bobjective:", "goal", 0.9),
    (r"\bconstraint:|\bmust not\b|\brequirement:", "constraint", 0.85),
    (r"\bwe decided\b|\blet'?s use\b|\badopted\b", "decision", 0.8),
    (r"\bdecision:", "decision", 0.9),
    (r"\bimportant:|\bnote:", "important", 0.6),
]


@dataclass
class Suggestion:
    message: Message
    tag: str
    confidence: float
    reason: str


def suggest(session: Session, min_confidence: float = 0.7) -> list[Suggestion]:
    out: list[Suggestion] = []
    for m in session.messages:
        if m.tags & {"goal", "constraint", "decision"}:
            continue
        text = m.content.lower()
        for pattern, tag, conf in PATTERNS:
            if conf < min_confidence:
                continue
            if re.search(pattern, text, re.I):
                out.append(Suggestion(m, tag, conf, f"matched /{pattern}/"))
                break
        else:
            if m.role == "user" and 0 < len(m.content.split()) <= 12 and m.content.rstrip().endswith(".") and re.match(r"^(build|fix|add|create|make|implement)\b", text):
                out.append(Suggestion(m, "goal", 0.65, "imperative goal-like message"))
    return out


def apply(session: Session, suggestions: list[Suggestion], keep_verbatim_for: set[str] | None = None) -> int:
    keep_verbatim_for = keep_verbatim_for or set()
    for s in suggestions:
        s.message.tags.add(s.tag)
        if s.tag in keep_verbatim_for:
            from .models import Policy

            s.message.policy = Policy.KEEP_VERBATIM
    return len(suggestions)

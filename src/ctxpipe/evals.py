"""Quality evals: does the compacted context still let the agent finish the task?

LLM-as-judge: the judge sees the task and the compacted view, and answers
YES/NO on whether the information needed to complete the task survived.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import re

from .core.models import Session
from .core.tokens import estimate_tokens, session_tokens
from .llm.base import LLM

JUDGE_SYSTEM = (
    "You are evaluating whether a compacted conversation transcript still contains "
    "everything needed to complete the given task. Your first line must be exactly "
    "YES or NO. Follow it with one sentence of explanation."
)

JUDGE_USER = "TASK:\n{task}\n\nCOMPACTED TRANSCRIPT:\n{transcript}"


@dataclass
class EvalResult:
    passed: bool
    explanation: str
    judge_raw: str
    metrics: dict = field(default_factory=dict)


def render_view(view: Session) -> str:
    lines = []
    for m in view.visible():
        tags = f" tags={','.join(sorted(m.tags))}" if m.tags else ""
        lines.append(f"[{m.role}{tags}] {m.content}")
    return "\n\n".join(lines)


def judge_prompt(task: str, view: Session) -> str:
    return JUDGE_USER.format(task=task, transcript=render_view(view))


def compute_metrics(original: Session, view: Session) -> dict:
    """Deterministic, LLM-free quality signals for a compacted view."""
    view_text = render_view(view)
    for m in view.messages:
        if m.meta.get("summary_json"):
            view_text += "\n" + m.meta["summary_json"]

    def retained(tag: str) -> int:
        return sum(1 for m in original.messages if tag in m.tags and m.content.strip() and m.content in view_text)

    error_tools = [
        m for m in original.messages
        if m.role == "tool" and re.search(r"\b(error|failed|failure|traceback|exception)\b", m.content, re.I)
    ]
    errors_kept = sum(1 for m in error_tools if m.content and m.content in view_text)
    before = session_tokens(original.visible()) + estimate_tokens(original.system)
    after = session_tokens(view.visible()) + estimate_tokens(view.system)
    return {
        "goals_retained": retained("goal"),
        "constraints_retained": retained("constraint"),
        "decisions_retained": retained("decision"),
        "errors_kept_verbatim": (errors_kept, len(error_tools)),
        "token_reduction": round(1 - after / before, 3) if before else 0.0,
        "tokens_before": before,
        "tokens_after": after,
    }


def judge(view: Session, task: str, llm: LLM, original: Session | None = None) -> EvalResult:
    raw = llm.complete(JUDGE_SYSTEM, judge_prompt(task, view), max_tokens=300).strip()
    first, _, rest = raw.partition("\n")
    result = EvalResult(
        passed=first.strip().upper().startswith("YES"),
        explanation=rest.strip(),
        judge_raw=raw,
    )
    if original is not None:
        result.metrics = compute_metrics(original, view)
    return result

"""Policy-aware compaction: plan -> summarize -> rebuild.

Rebuilt context order: pinned messages (verbatim), compaction summary, recent N messages.
System prompt and tool schemas ride along on the Session unchanged.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from .scopes import ScopeRule, scope_matches, specificity
from ..llm.base import LLM
from .models import Message, Policy, Rule, Session
from .prompts import COMPACTION_USER, SUMMARY_HEADER
from .summary import format_summary_block, parse_structured_summary
from .tokens import estimate_tokens, session_tokens

SUMMARY_TAG = "compaction_summary"


@dataclass
class CompactionPlan:
    excluded: list[Message] = field(default_factory=list)      # drop_always
    pinned: list[Message] = field(default_factory=list)        # kept verbatim
    to_summarize: list[Message] = field(default_factory=list)
    dropped: list[Message] = field(default_factory=list)       # drop_ok / no_errors
    recent: list[Message] = field(default_factory=list)        # last N, verbatim

    def tokens_before(self) -> int:
        return session_tokens(self.pinned + self.to_summarize + self.dropped + self.recent)


def _fix_pair_boundary(visible: list[Message], split: int) -> int:
    """Move the summary/recent split so a tool_use/tool_result pair is never split."""
    where: dict[str, list[int]] = {}
    for i, m in enumerate(visible):
        for pid in m.pair_ids():
            where.setdefault(pid, []).append(i)
    while True:
        moved = False
        for idxs in where.values():
            if len(idxs) < 2:
                continue
            on_left = [i for i in idxs if i < split]
            on_right = [i for i in idxs if i >= split]
            if on_left and on_right:
                split = min(split, min(idxs))
                moved = True
        if not moved:
            return split


def _msg_path(m: Message) -> str | None:
    return m.meta.get("path") or m.meta.get("file")


def scope_rule(msg: Message, scopes: list[ScopeRule]) -> Rule:
    """Winning scope rule for msg: highest specificity, then strictest, then first-listed."""
    hits = [s for s in scopes if scope_matches(s.match, msg, _msg_path(msg))]
    if not hits:
        return Rule()
    top_spec = max(specificity(s.match) for s in hits)
    ties = [s for s in hits if specificity(s.match) == top_spec]
    return max(ties, key=lambda s: s.rule.strictness()).rule


def effective_rule(msg: Message, scopes: list[ScopeRule]) -> Rule:
    return scope_rule(msg, scopes).merged_with(msg.rule)


def result_has_errors(m: Message) -> bool:
    import re

    if m.meta.get("exit_code") not in (None, 0, "0"):
        return True
    return re.search(r"\b(error|failed|failure|traceback|exception)\b", m.content, re.I) is not None


def truncate_result(content: str, rule: Rule) -> str:
    if rule.keep_result_sections:
        import re

        kept: list[str] = []
        capture = False
        for line in content.splitlines():
            h = re.match(r"^\s*#+\s*(.+)$", line)
            if h:
                capture = any(s.lower() in h.group(1).lower() for s in rule.keep_result_sections)
            if capture:
                kept.append(line)
        content = "\n".join(kept) if kept else content
    if rule.truncate_result_to is not None and estimate_tokens(content) > rule.truncate_result_to:
        from .tokens import _ENC

        n = rule.truncate_result_to
        if _ENC is not None:
            ids = _ENC.encode(content, disallowed_special=())
            head = ids[: n // 2]
            tail = ids[-n // 2 :] if n >= 2 else []
            content = _ENC.decode(head) + "\n…\n" + _ENC.decode(tail)
        else:
            chars = n * 4
            content = content[:chars] + "\n…\n" + content[-chars:]
    return content


def plan(session: Session, keep_recent: int = 6, scopes: list[ScopeRule] | None = None
         ) -> CompactionPlan:
    scopes = scopes or []
    compaction_count = len(session.meta.get("compactions", []))
    p = CompactionPlan(excluded=[m for m in session.messages if m.policy is Policy.DROP_ALWAYS])
    visible = session.visible()
    split = max(0, len(visible) - keep_recent)
    split = _fix_pair_boundary(visible, split)
    older, p.recent = visible[:split], visible[split:]

    idx_in_session = {id(m): i for i, m in enumerate(session.messages)}
    for m in older:
        rule = effective_rule(m, scopes)
        age = len(session.messages) - idx_in_session[id(m)]

        pinned = m.is_pinned
        if rule.keep_verbatim_until is not None:
            pinned = pinned or compaction_count < rule.keep_verbatim_until
        # pair-level: a strict keep_result rule from partner pins the pair too
        if pinned:
            p.pinned.append(m)
            continue

        droppable = m.policy is Policy.DROP_OK and (
            rule.drop_ok_after is None or age >= rule.drop_ok_after
        )
        if rule.drop_result_if == "always" and m.role == "tool":
            droppable = True
        elif rule.drop_result_if == "no_errors" and m.role == "tool" and not result_has_errors(m):
            droppable = True

        if droppable:
            p.dropped.append(m)
        else:
            if m.role == "tool" and rule.truncate_result_to is not None:
                m = _transform(m, rule)
            p.to_summarize.append(m)
    return p


def _transform(m: Message, rule: Rule) -> Message:
    import dataclasses

    return dataclasses.replace(m, content=truncate_result(m.content, rule))


def _render(messages: list[Message], scopes: list[ScopeRule] | None = None) -> str:
    if not messages:
        return "(none)"
    parts = []
    for m in messages:
        detail = effective_rule(m, scopes or []).detail() if scopes else (m.rule.detail() if m.rule else "medium")
        parts.append(
            f'<message id="{m.id}" role="{m.role}" tags="{",".join(sorted(m.tags))}" '
            f'policy="{m.policy.value}" detail="{detail}">\n{m.content}\n</message>'
        )
    return "\n\n".join(parts)


def summarize(p: CompactionPlan, llm: LLM, target_tokens: int = 1500,
              previous_summary: dict | None = None, system_prompt_text: str | None = None,
              profile: str = "balanced", scopes: list[ScopeRule] | None = None) -> str:
    if not p.to_summarize:
        return ""
    if system_prompt_text is not None:
        system = system_prompt_text.format(target_tokens=target_tokens)
    else:
        from .prompts import system_prompt

        system = system_prompt(profile, target_tokens)
    previous = ""
    if previous_summary is not None:
        import json

        previous = (
            "\n\nPrevious structured summary (from earlier compaction) — treat as the\n"
            "baseline state; re-emit and extend it, preserving goals/constraints/decisions\n"
            "unless newer turns explicitly supersede them, and do not duplicate content:\n"
            + json.dumps(previous_summary, indent=2)
        )
    user = COMPACTION_USER.format(
        pinned=_render(p.pinned, scopes), to_summarize=_render(p.to_summarize, scopes)
    ) + previous
    raw = llm.complete(system, user, max_tokens=target_tokens * 2).strip()
    if parse_structured_summary(raw) is None:
        retry_user = user + "\n\nReturn ONLY valid JSON following the schema, no prose."
        raw2 = llm.complete(system, retry_user, max_tokens=target_tokens * 2).strip()
        if parse_structured_summary(raw2) is not None:
            return raw2
        return raw  # legacy free-form fallback; rebuild will still embed it
    return raw


def rebuild(session: Session, p: CompactionPlan, summary: str,
            token_budget: int = 1500, scopes: list[ScopeRule] | None = None) -> Session:
    """Return the agent-visible, compacted view. The master session is not mutated."""
    out = Session(
        agent=session.agent, project=session.project, id=session.id,
        created_at=session.created_at, system=session.system,
        tool_schemas=session.tool_schemas, meta=dict(session.meta),
    )
    out.messages.extend(_transform_view(m, scopes) for m in p.pinned)
    if summary:
        data = parse_structured_summary(summary)
        if data is not None:
            content = SUMMARY_HEADER + format_summary_block(data, token_budget)
            meta = {"summary_json": summary}
        else:
            content = SUMMARY_HEADER + summary
            meta = {}
        out.messages.append(Message(
            role="user", content=content, tags={SUMMARY_TAG}, meta=meta,
        ))
    out.messages.extend(_transform_view(m, scopes) for m in p.recent)
    return out


def _transform_view(m: Message, scopes: list[ScopeRule] | None) -> Message:
    if m.role != "tool":
        return m
    rule = effective_rule(m, scopes or [])
    if rule.truncate_result_to is not None or rule.keep_result_sections:
        import dataclasses

        return dataclasses.replace(m, content=truncate_result(m.content, rule))
    return m


def tokens_after(view: Session) -> int:
    return session_tokens(view.messages) + estimate_tokens(view.system)

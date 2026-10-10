"""Automatic compaction: config, assessment, state machine, checkpoint,
essence directives, verification, circuit breakers, hysteresis.

Deterministic core, no I/O. Terminal interaction and subprocess wiring live
in cli.py / wrapper.py.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal

from .compaction import plan, rebuild
from .models import Message, Policy, Session
from .summary import parse_structured_summary
from .tokens import estimate_tokens, session_tokens

# ---------------------------------------------------------------- config


@dataclass
class AutoCompactConfig:
    enabled: bool = False  # backward compatible: off unless explicitly enabled
    evaluate_at_percent: int = 65
    recommend_at_percent: int = 75
    force_at_percent: int = 90
    rearm_below_percent: int = 45

    use_effective_pressure: bool = True
    reserve_output_tokens: int = 12_000
    reserve_tool_tokens: int = 16_000

    context_window: int = 200_000
    min_projected_reduction: float = 0.30
    min_turns_since_compaction: int = 12
    min_new_tokens: int = 15_000
    cooldown_turns: int = 12

    ask_user: Literal["always", "smart", "never", "critical-only"] = "smart"
    default_action: Literal["postpone", "skip", "apply"] = "postpone"
    noninteractive_action: Literal["postpone", "apply-if-valid", "disable"] = "postpone"

    max_semantic_risk_without_confirmation: Literal["low", "medium", "high", "critical"] = "low"
    verify_before_apply: bool = True
    rollback_on_failure: bool = True

    keep_recent: int = 6
    max_attempts: int = 2
    min_reduction: float = 0.25

    def validate(self) -> None:
        if self.enabled and not (0 < self.rearm_below_percent < self.evaluate_at_percent
                                 < self.recommend_at_percent < self.force_at_percent <= 100):
            raise ValueError(
                "invalid auto_compaction thresholds: require "
                "0 < rearm_below < evaluate_at < recommend_at < force_at <= 100, got "
                f"rearm={self.rearm_below_percent} evaluate={self.evaluate_at_percent} "
                f"recommend={self.recommend_at_percent} force={self.force_at_percent}"
            )
        if self.enabled and (not self.use_effective_pressure or self.context_window <= 0):
            raise ValueError("auto_compaction needs a positive context_window")


# ------------------------------------------------------------- state machine

STATES = (
    "IDLE", "WATCHING", "ELIGIBLE", "PENDING_BOUNDARY", "BUILDING_CANDIDATE",
    "WAITING_FOR_USER", "VERIFYING", "REPAIRING", "APPLIED", "REJECTED",
    "COOLDOWN", "EMERGENCY",
)
TERMINALISH = {"APPLIED", "REJECTED", "IDLE"}

TRANSITIONS = {
    "IDLE": {"WATCHING", "EMERGENCY"},
    "WATCHING": {"ELIGIBLE", "IDLE", "EMERGENCY"},
    "ELIGIBLE": {"PENDING_BOUNDARY", "BUILDING_CANDIDATE", "WATCHING", "EMERGENCY"},
    "PENDING_BOUNDARY": {"BUILDING_CANDIDATE", "ELIGIBLE", "REJECTED", "EMERGENCY"},
    "BUILDING_CANDIDATE": {"WAITING_FOR_USER", "VERIFYING", "REJECTED", "EMERGENCY"},
    "WAITING_FOR_USER": {"VERIFYING", "REJECTED", "BUILDING_CANDIDATE", "EMERGENCY"},
    "VERIFYING": {"APPLIED", "REPAIRING", "REJECTED", "EMERGENCY"},
    "REPAIRING": {"VERIFYING", "WAITING_FOR_USER", "REJECTED", "EMERGENCY"},
    "APPLIED": {"COOLDOWN", "EMERGENCY"},
    "REJECTED": {"WATCHING", "COOLDOWN", "EMERGENCY"},
    "COOLDOWN": {"WATCHING", "EMERGENCY"},
    "EMERGENCY": {"VERIFYING", "REJECTED"},
}


class StateMachine:
    def __init__(self, state: str = "IDLE") -> None:
        if state not in STATES:
            state = "IDLE"
        self.state = state

    def transition(self, to: str) -> None:
        if to not in TRANSITIONS.get(self.state, set()):
            raise ValueError(f"illegal transition {self.state} -> {to}")
        self.state = to

    def try_transition(self, to: str) -> bool:
        if to in TRANSITIONS.get(self.state, set()):
            self.state = to
            return True
        return False


# -----------------------------------------------------------------assessment

RISK_LEVELS = ("low", "medium", "high", "critical")


@dataclass
class CompactionAssessment:
    current_tokens: int
    usable_context_tokens: int
    effective_pressure: float
    projected_output_tokens: int
    projected_reduction: float
    semantic_risk: Literal["low", "medium", "high", "critical"]
    recommendation: Literal["skip", "wait", "ask", "apply", "emergency"]
    reasons: list[str]
    blockers: list[str]
    removable_tool_ratio: float
    pinned_content_ratio: float
    completed_work_ratio: float
    exact_fact_density: float


def _system_overhead(s: Session) -> int:
    schemas = sum(estimate_tokens(json.dumps(t)) for t in s.tool_schemas)
    return estimate_tokens(s.system) + schemas


def _exact_fact_density(s: Session) -> float:
    if not s.messages:
        return 0.0
    hits = 0
    for m in s.messages:
        if re.search(r"[A-Za-z]:[\\/]|\.py\b|\.ts\b|\.json\b|\d+\.\d+|\b[a-f0-9]{7,}\b|`[^`]+`", m.content):
            hits += 1
    return hits / len(s.messages)


def _removable_tool_ratio(s: Session) -> float:
    tools = [m for m in s.messages if m.role == "tool"]
    if not tools:
        return 0.0
    ok = [m for m in tools if not re.search(r"\b(error|failed|failure|traceback|exception)\b", m.content, re.I)
          and m.meta.get("exit_code") in (None, 0, "0")]
    return len(ok) / len(tools)


def _completed_ratio(s: Session) -> float:
    if not s.messages:
        return 0.0
    done = sum(1 for m in s.messages if "done" in m.content.lower() or "passed" in m.content.lower()
               or m.meta.get("status") == "done")
    return done / len(s.messages)


def _open_pair_ids(s: Session) -> set[str]:
    open_ids: set[str] = set()
    seen: dict[str, set[str]] = {}
    for m in s.messages:
        for pid in m.pair_ids():
            seen.setdefault(pid, set()).add("tool" if m.role == "tool" else "call")
    return {pid for pid, roles in seen.items() if roles != {"tool", "call"}}


def is_safe_boundary(s: Session, checkpoint: dict | None = None) -> bool:
    """False when mid tool pair, mid assistant/tool stream, or risky phase."""
    if not s.messages:
        return False
    last = s.messages[-1]
    # assistant message promising tool calls with no result yet
    if last.role == "assistant" and ("[tool_use" in last.content or last.pair_ids()):
        last_ids = set(last.pair_ids()) or _guess_tool_ids(last.content)
        if last_ids and not any((set(m.pair_ids()) & last_ids) and m.role == "tool" for m in s.messages[-3:]):
            return False
    if last.role == "tool":
        # a tool result whose call arrived but whose pair group may continue
        follows = [m for m in s.messages if set(m.pair_ids()) & set(last.pair_ids())]
        if len({m.role for m in follows}) > 2:  # >>2 means group still growing
            return False
    if _open_pair_ids(s):
        return False
    if checkpoint:
        phase = str(checkpoint.get("phase", "")).lower()
        if any(p in phase for p in ("migrat", "rebase", "commit", "approval", "deploy")):
            return False
    return True


def _guess_tool_ids(content: str) -> set[str]:
    return set(re.findall(r"toolu_\w+|call_\w+", content))


def assess(s: Session, cfg: AutoCompactConfig, keep_recent: int | None = None,
           scopes=None, checkpoint: dict | None = None) -> CompactionAssessment:
    if keep_recent is None:
        keep_recent = cfg.keep_recent
    visible = s.visible()
    current = session_tokens(visible) + (estimate_tokens(s.system) if cfg.use_effective_pressure else 0)
    overhead = _system_overhead(s) if cfg.use_effective_pressure else 0
    open_pairs = _open_pair_ids(s)
    expected_tool = cfg.reserve_tool_tokens if open_pairs else 0
    numerator = current + overhead + cfg.reserve_output_tokens + expected_tool
    usable = max(1, cfg.context_window - overhead)
    pressure = numerator / (usable + overhead)

    p = plan(s, keep_recent=keep_recent, scopes=scopes or [])
    dropped_tokens = session_tokens(p.dropped)
    summarized = session_tokens(p.to_summarize)
    summary_estimate = max(1, int(summarized * 0.25)) if summarized else 0
    pinned_recent = session_tokens(p.pinned + p.recent) + estimate_tokens(s.system)
    projected = pinned_recent + summary_estimate
    reduction = 1 - projected / max(1, current)
    reasons: list[str] = []
    blockers: list[str] = []
    removable = _removable_tool_ratio(s)
    pinned_ratio = (session_tokens(p.pinned) / max(1, current))

    if blocked_reason := _skip_reason(s, cfg):
        blockers.append(blocked_reason)
    if removable > 0.5:
        reasons.append(f"{int(removable * 100)}% of tool output is disposable")
    if reduction < cfg.min_projected_reduction and not blockers:
        blockers.append(f"projected reduction {reduction:.0%} below {cfg.min_projected_reduction:.0%}")
    projected_pressure = (projected + overhead + cfg.reserve_output_tokens + expected_tool) / max(1, usable + overhead)
    if projected_pressure >= cfg.evaluate_at_percent / 100 and pressure < cfg.force_at_percent / 100:
        blockers.append("candidate would immediately retrigger compaction")
    if pinned_ratio > 0.8:
        blockers.append("protected content exceeds budget")
    meta_ac = s.meta.get("auto_compaction", {})
    if int(meta_ac.get("failed_attempts", 0)) >= 1:
        reasons.append(f"{meta_ac.get('failed_attempts')} earlier automatic attempt(s) failed")
    if s.messages and re.search(r"\b(done|passed|committed|merged)\b", s.messages[-1].content, re.I) \
            and not open_pairs:
        reasons.append("natural task boundary")
    if cfg.recommend_at_percent / 100 <= pressure < cfg.force_at_percent / 100:
        reasons.append(f"above recommend threshold ({cfg.recommend_at_percent}%)")
    if not is_safe_boundary(s, checkpoint):
        blockers.append("no safe boundary yet (open pair or risky phase)")
        recommendation = "wait"
    else:
        recommendation = _recommendation(cfg, pressure, reduction, pinned_ratio, s, checkpoint)
    risk_score = _risk_score(s, p, checkpoint, pinned_ratio)
    risk = RISK_LEVELS[min(3, risk_score)]
    if pressure >= cfg.force_at_percent / 100:
        recommendation = "emergency"
        reasons.append(f"above force threshold ({cfg.force_at_percent}%)")
    if blockers and "no safe boundary" not in blockers[0]:
        recommendation = "skip" if "projected reduction" in blockers[0] or reduction < cfg.min_projected_reduction else "wait"
    if blockers and blockers[0].startswith("no safe boundary"):
        recommendation = "wait"
    if any(b.startswith(("candidate would immediately", "protected content")) for b in blockers):
        recommendation = "skip"
    return CompactionAssessment(
        current_tokens=current, usable_context_tokens=usable, effective_pressure=round(pressure, 3),
        projected_output_tokens=projected, projected_reduction=round(reduction, 3),
        semantic_risk=risk, recommendation=recommendation, reasons=reasons, blockers=blockers,
        removable_tool_ratio=round(removable, 3), pinned_content_ratio=round(pinned_ratio, 3),
        completed_work_ratio=round(_completed_ratio(s), 3), exact_fact_density=round(_exact_fact_density(s), 3),
    )


def _skip_reason(s: Session, cfg: AutoCompactConfig) -> str | None:
    meta = s.meta.get("auto_compaction", {})
    last_idx = int(meta.get("last_compaction_msg_count", 0))
    turns_since = len(s.messages) - last_idx
    tokens_since = session_tokens(s.messages[last_idx:])
    if meta.get("disabled_session"):
        return "disabled for this session"
    if turns_since < cfg.min_turns_since_compaction and last_idx:
        return f"only {turns_since} turns since last compaction (< {cfg.min_turns_since_compaction})"
    if last_idx and tokens_since < cfg.min_new_tokens:
        return f"only {tokens_since} new tokens since last compaction (< {cfg.min_new_tokens})"
    return None


def _risk_score(s, p, ckpt, pinned_ratio) -> int:
    score = 0
    if pinned_ratio > 0.5:
        score += 2
    if ckpt:
        if ckpt.get("pending_steps"):
            score += 2
        if ckpt.get("files_modified_by_user"):
            score += 1
        if not ckpt.get("next_action") or ckpt.get("next_action") == "unknown":
            score += 1
        if ckpt.get("unresolved_questions"):
            score += 1
    decisions_tagged = sum(1 for m in s.messages if "decision" in m.tags)
    if decisions_tagged > 8:
        score += 1
    if _open_pair_ids(s):
        score += 1
    if int(s.meta.get("auto_compaction", {}).get("failed_attempts", 0)) >= 1:
        score += 1
    if s.messages and s.messages[0].tags and "compaction_summary" in s.messages[0].tags:
        score += 1
    return score


def _recommendation(cfg, pressure, reduction, pinned_ratio, s, ckpt) -> Literal["skip", "wait", "ask", "apply"]:
    if pressure < cfg.evaluate_at_percent / 100:
        return "skip"
    incomplete = bool(ckpt and (ckpt.get("pending_steps") or ckpt.get("phase") in ("implementation", "testing", "review")))
    conflicting = bool(ckpt and ckpt.get("unresolved_questions"))
    needs_ask = (
        incomplete or conflicting or pinned_ratio > 0.6
        or (ckpt and ckpt.get("files_modified_by_user"))
        or (ckpt and not ckpt.get("next_action"))
    )
    if pressure >= cfg.recommend_at_percent / 100:
        return "ask" if needs_ask else "apply"
    # 65-75
    if reduction >= cfg.min_projected_reduction and not needs_ask:
        return "apply"
    if needs_ask:
        return "ask"
    return "wait"


# --------------------------------------------------------------- continuity


CHECKPOINT_TAG = "continuity_checkpoint"
_PATH = r"[\w./\\-]+\.\w+"


def _cp_messages(s: Session) -> list[Message]:
    """Messages a checkpoint may read: visible (never drop_always), not an old checkpoint."""
    return [m for m in s.visible() if CHECKPOINT_TAG not in m.tags]


def capture_checkpoint(s: Session) -> dict:
    """Deterministic continuity state. Unknown values stay 'unknown' / [] / None."""
    msgs = _cp_messages(s)
    phases = {"discussion": "discussion", "investigat": "investigation", "plan": "planning",
              "implement": "implementation", "test": "testing", "review": "review"}
    recent = msgs[-6:]
    text = " ".join(m.content.lower() for m in recent)
    phase = next((v for k, v in phases.items() if k in text), "unknown")
    user_goal = next((m.content for m in msgs if "goal" in m.tags), "")
    last_done = next((m.content for m in reversed(msgs) if "passed" in m.content.lower() or "done" in m.content.lower()), "")
    files = sorted({f for m in msgs for f in re.findall(r"[\w./\\-]+\.(?:py|ts|js|md|yaml|yml|json)", m.content)})
    failing = [m.content[:120] for m in msgs if m.role == "tool" and re.search(r"\b(error|failed|failure|traceback|exception)\b", m.content, re.I)]

    last10 = " ".join(m.content.lower() for m in msgs[-10:])
    if "git push" in last10 or "push_allowed" in last10:
        permission = "push_allowed"
    elif "commit" in last10:
        permission = "commit_allowed"
    elif any(m.role == "tool" for m in msgs[-10:]):
        permission = "commands_allowed"
    elif re.search(r"\b(edit|write file|apply patch)\b", last10):
        permission = "edit_allowed"
    else:
        permission = "unknown"

    by_agent = sorted({f for m in msgs if m.role == "assistant"
                       for f in re.findall(r"\b(?:edited|wrote|created|modified)\s+(" + _PATH + ")", m.content, re.I)})
    by_user = sorted({f for m in msgs if m.role == "user"
                      for f in re.findall(r"\bI\s+(?:just\s+)?(?:modified|changed|edited)\s+(" + _PATH + ")", m.content, re.I)})
    pending: list[str] = []
    if msgs:
        for line in msgs[-1].content.splitlines():
            ls = line.strip()
            if ls.startswith("- [ ]"):
                pending.append(ls[5:].strip())
            elif ls.upper().startswith("TODO:"):
                pending.append(ls[5:].strip())
    nxt = "unknown"
    for m in reversed(recent):
        mm = re.search(r"\bnext(?: step)?:\s*(.+)", m.content, re.I)
        if mm:
            nxt = mm.group(1).strip().splitlines()[0]
            break
    return {
        "objective": user_goal or "unknown",
        "phase": phase,
        "action_permission": permission,
        "active_step": "unknown",
        "next_action": nxt,
        "stop_condition": "",
        "completed_steps": [last_done] if last_done else [],
        "pending_steps": pending,
        "blocked_steps": [],
        "files_inspected": files[:20],
        "files_modified_by_agent": by_agent,
        "files_modified_by_user": by_user,
        "latest_tests": {"passed": None, "failed": len(failing) if failing else None, "failing_names": []},
        "known_errors": failing[-5:],
        "rejected_approaches": [],
        "unresolved_questions": [],
        "last_successful_action": last_done or "unknown",
        "resume_requires_confirmation": False,
    }


def checkpoint_id(ckpt: dict) -> str:
    return hashlib.sha256(json.dumps(ckpt, sort_keys=True, default=str).encode()).hexdigest()[:12]


def checkpoint_message(ckpt: dict) -> Message:
    """Compact, high-priority continuity block (empty/unknown fields omitted)."""
    empty_tests = {"passed": None, "failed": None, "failing_names": []}
    slim = {k: v for k, v in ckpt.items()
            if v not in ("", "unknown", [], None, False) and v != empty_tests}
    return Message(role="user", content="## Continuity checkpoint\n" + json.dumps(slim, separators=(",", ":")),
                   tags={CHECKPOINT_TAG}, policy=Policy.KEEP_VERBATIM)


def insert_checkpoint(messages: list[Message], ckpt: dict) -> list[Message]:
    """Drop any older checkpoint block and place a fresh one right after the summary."""
    kept = [m for m in messages if CHECKPOINT_TAG not in m.tags]
    idx = 0
    for i, m in enumerate(kept):
        if "compaction_summary" in m.tags:
            idx = i + 1
    kept.insert(idx, checkpoint_message(ckpt))
    return kept


# ----------------------------------------------------------------- essence


def add_essence(s: Session, content: str, scope: str = "next_compaction") -> dict:
    d = {
        "type": "essence_directive", "content": content, "scope": scope,
        "policy": "must_preserve", "source": "user",
        "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"),
        "created_at_usage": None,
    }
    d["id"] = hashlib.sha256((content + d["created_at"]).encode()).hexdigest()[:8]
    s.meta.setdefault("essence_directives", []).append(d)
    return d


def pending_essences(s: Session) -> list[dict]:
    return [d for d in s.meta.get("essence_directives", []) if d.get("scope") in ("next_compaction", "session", "permanent")]


def expire_next_compaction(s: Session) -> None:
    s.meta["essence_directives"] = [d for d in s.meta.get("essence_directives", []) if d.get("scope") != "next_compaction"]


def parse_essence_input(line: str) -> tuple[str | None, str]:
    """Returns (directive_or_None, action) where action in {apply, preview, none, question}.

    Conservative: bare free text is treated as a directive (action 'question').
    """
    t = line.strip()
    low = t.lower()
    for suffix, action in (("; apply", "apply"), ("; preview", "preview"), ("; later", "later")):
        if low.endswith(suffix):
            return t[: -len(suffix)].strip().removeprefix("keep ").strip(), action
    if low.startswith("keep "):
        return t[5:].strip(), "question"
    return t or None, "question"


# ------------------------------------------------------------- verification

_ERROR_RE = re.compile(r"\b(error|failed|failure|traceback|exception)\b", re.I)


@dataclass
class Verification:
    passed: bool
    errors: list[str]
    warnings: list[str]
    repair_attempted: bool = False


_FACT_RE = re.compile(r"[\w./\\-]+\.(?:py|ts|js|json|yaml|yml)\b|`[^`\n]{2,60}`|\b[a-f0-9]{7,40}\b")


def collect_exact_facts(s: Session) -> list[str]:
    """Exact identifiers (paths, `code`, hex ids) from pinned / goal / constraint / decision messages."""
    facts: list[str] = []
    for m in s.visible():
        if CHECKPOINT_TAG in m.tags:
            continue
        if m.is_pinned or m.tags & {"decision", "goal", "constraint"}:
            for f in _FACT_RE.findall(m.content):
                if f not in facts:
                    facts.append(f)
    return facts


def _summary_claims(candidate: Session) -> list[tuple[str, str]]:
    claims: list[tuple[str, str]] = []
    for m in candidate.messages:
        raw = m.meta.get("summary_json")
        if not raw or "compaction_summary" not in m.tags:
            continue
        data = parse_structured_summary(raw)
        if not data:
            continue
        high = data.get("summary_high", {})
        for key in ("goals", "constraints", "decisions"):
            for item in high.get(key, []):
                text = item.get("what", "") if isinstance(item, dict) else str(item)
                if text.strip():
                    claims.append((key, text))
    return claims


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]{3,}", text.lower())}


def verify(original: Session, candidate: Session, essences: list[dict],
           cfg: AutoCompactConfig, reduction: float,
           checkpoint: dict | None = None, exact_facts: list[str] | None = None) -> Verification:
    errors: list[str] = []
    warnings: list[str] = []
    c_text = "\n".join(m.content for m in candidate.messages)
    for m in candidate.messages:
        if m.meta.get("summary_json"):
            c_text += "\n" + m.meta["summary_json"]

    for m in original.messages:
        if CHECKPOINT_TAG in m.tags:
            continue  # replaced by a fresh checkpoint on every compaction
        if m.policy is Policy.KEEP_VERBATIM or m.tags & {"goal", "constraint"}:
            if m.content.strip() and m.content not in c_text:
                errors.append(f"pinned message lost: {m.content[:60]!r}")
        if m.policy is Policy.DROP_ALWAYS and m.content.strip() and m.content in c_text:
            errors.append("drop_always content leaked into candidate")

    for d in essences:
        for fragment in re.split(r"[,;]", d.get("content", "")):
            frag = fragment.strip()
            if frag and frag.lower() not in c_text.lower():
                errors.append(f"essence directive not satisfied: {frag[:60]!r}")

    # tool pair integrity in candidate
    groups: dict[str, set[str]] = {}
    for m in candidate.messages:
        for pid in m.pair_ids():
            groups.setdefault(pid, set()).add(m.role)
    for pid, roles in groups.items():
        if "call" in roles or "tool" in roles:
            if not ({"tool"} & roles and any(r != "tool" for r in roles)):
                warnings.append(f"pair {pid} may be incomplete")

    # continuity checkpoint must survive (task 6)
    if checkpoint is not None:
        if not any(CHECKPOINT_TAG in m.tags for m in candidate.messages):
            errors.append("continuity checkpoint missing")
        else:
            for key in ("next_action", "action_permission"):
                val = checkpoint.get(key)
                if val not in (None, "", "unknown") and str(val) not in c_text:
                    errors.append(f"{key} not preserved")

    # exact facts must survive exactly (task 7)
    for fact in exact_facts or []:
        if fact not in c_text:
            errors.append(f"exact fact lost: {fact[:60]!r}")

    # summary may not claim goals/constraints/decisions the transcript never contained (task 7)
    orig_words: set[str] = set()
    for m in original.messages:
        if m.policy is not Policy.DROP_ALWAYS:
            orig_words |= _words(m.content)
    for key, text in _summary_claims(candidate):
        w = _words(text)
        if w and len(w & orig_words) / len(w) < 0.6:
            errors.append(f"unsupported {key[:-1]}: {text[:60]!r}")

    # specific completed item regressed to pending (task 7)
    for m in original.messages:
        if m.role != "tool" and re.search(r"\b(passed|done)\b", m.content, re.I):
            key = re.sub(r"\s+", " ", m.content.strip().lower())[:40]
            if len(key) >= 12 and re.search(r"\b(pending|todo|open task)\b[^\n]{0,80}" + re.escape(key), c_text.lower()):
                errors.append(f"completed-task regression: {key!r} listed as pending")

    # completed-task regression: any '完成/passed/done' message from original should not mark pending
    if any("passed" in m.content.lower() or "done" in m.content.lower() for m in original.messages):
        if re.search(r"\bpending\b.*\bfailed\b", c_text, re.I):
            errors.append("completed-task regression: completed work appears regressed to pending")

    if reduction < cfg.min_reduction:
        errors.append(f"reduction {reduction:.0%} below required {cfg.min_reduction:.0%}")

    # candidate must not immediately retrigger
    cand_tokens = session_tokens(candidate.visible())
    if candidate.messages:
        est_pressure = (cand_tokens + cfg.reserve_output_tokens + cfg.reserve_tool_tokens) / cfg.context_window
        if est_pressure >= cfg.force_at_percent / 100:
            errors.append("candidate still above force threshold")

    for m in candidate.messages:
        if "compaction_summary" in m.tags and m.meta.get("summary_json"):
            if parse_structured_summary(m.meta["summary_json"]) is None:
                errors.append("summary_json is not valid structured summary")

    if not any(m.tags & {"goal", "constraint"} for m in original.messages):
        warnings.append("no goal/constraint tags found; pinning coverage unknown")

    return Verification(passed=not errors, errors=errors, warnings=warnings)


def repair(candidate: Session, original: Session, essences: list[dict],
           checkpoint: dict | None = None) -> Session:
    """Deterministic one-shot repair: append missing essence to the summary block,
    re-add dropped drop_always-safe pinned messages, remove leaked drop_always text."""
    out = Session(agent=candidate.agent, project=candidate.project, id=candidate.id,
                  created_at=candidate.created_at, system=candidate.system,
                  tool_schemas=candidate.tool_schemas, meta=dict(candidate.meta))
    out.messages = list(candidate.messages)
    joined = "\n".join(m.content for m in out.messages)
    missing = []
    for d in essences:
        for fragment in re.split(r"[,;]", d.get("content", "")):
            frag = fragment.strip()
            if frag and frag.lower() not in joined.lower():
                missing.append(frag)
    if missing and out.messages:
        for m in out.messages:
            if "compaction_summary" in m.tags:
                m.content += "\n\n## Essence (user directive)\n" + "\n".join(f"- {x}" for x in missing)
                break
    for m in list(out.messages):
        if m.policy is Policy.DROP_ALWAYS:
            out.messages.remove(m)
    leaked = [m for m in original.messages if m.policy is Policy.DROP_ALWAYS]
    for m in out.messages:
        for lm in leaked:
            if lm.content.strip() and lm.content in m.content:
                m.content = m.content.replace(lm.content, "[redacted]")
    joined2 = "\n".join(m.content for m in out.messages)
    pinned_missing = []
    for m in original.messages:
        if (m.policy is Policy.KEEP_VERBATIM or m.tags & {"goal", "constraint"}) and m.content.strip() and m.content not in joined2:
            pinned_missing.append(m)
    if pinned_missing:
        out.messages = [m for m in pinned_missing if CHECKPOINT_TAG not in m.tags] + out.messages
    if checkpoint is not None and not any(CHECKPOINT_TAG in m.tags for m in out.messages):
        out.messages = insert_checkpoint(out.messages, checkpoint)
    return out


# ----------------------------------------------------- controller / orchestration


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_reason(errors: list[str]) -> str:
    """Error categories only (text before ':'), so audit/history never carry message content."""
    return "; ".join(sorted({e.split(":")[0].strip() for e in errors}))[:200]


def _say(ui: Any, text: str) -> None:
    fn = getattr(ui, "info", None)
    if callable(fn):
        fn(text)
    else:
        print(text)


# ------------------------------------------------------------------- review views


def review_summary(p, risk: str | None = None) -> str:
    out = (f"KEEP:      {len(p.pinned) + len(p.recent)} messages\n"
           f"SUMMARIZE: {len(p.to_summarize)} messages\n"
           f"DROP:      {len(p.dropped)} messages\n")
    if risk:
        out += f"RISK:      {risk.upper()}\n"
    return out


def review_items(s: Session, p) -> list[str]:
    lines: list[str] = []
    for m in p.pinned:
        kind = next(iter(sorted(m.tags & {"goal", "constraint", "decision"})), m.policy.value)
        lines.append(f"KEEP  {m.id}  {kind:<10}  {m.content[:50]}")
    for m in p.recent:
        lines.append(f"KEEP  {m.id}  {'recent':<10}  {m.content[:50]}")
    if p.to_summarize:
        first, last = p.to_summarize[0].id, p.to_summarize[-1].id
        lines.append(f"SUM   {first}-{last}  {len(p.to_summarize)} messages -> one structured summary")
    for m in p.dropped:
        ref = f"pair:{sorted(m.pair_ids())[0]}" if m.pair_ids() else m.id
        lines.append(f"DROP  {ref}  {m.policy.value}  {m.content[:40]}")
    return lines


def _claim_text(item: Any) -> str:
    return str(item.get("what", item)) if isinstance(item, dict) else str(item)


def review_diff(old: dict | None, new: dict | None) -> str:
    """Structured diff of summary_high lists (old = previous summary, new = candidate)."""
    if new is None:
        return "(no candidate summary available yet)"
    old_high = (old or {}).get("summary_high", {}) or {}
    new_high = new.get("summary_high", {}) or {}
    out: list[str] = []
    for key in list(dict.fromkeys([*old_high.keys(), *new_high.keys()])):
        o = [_claim_text(x) for x in old_high.get(key, []) or []]
        n = [_claim_text(x) for x in new_high.get(key, []) or []]
        removed = [x for x in o if x not in n]
        added = [x for x in n if x not in o]
        if removed or added:
            out.append(f" {key}:")
            out += [f"- {x}" for x in removed] + [f"+ {x}" for x in added]
    return "\n".join(out) if out else "(no structural changes)"


def _summary_high_of(s: Session) -> dict | None:
    for m in reversed(s.messages):
        if "compaction_summary" in m.tags and m.meta.get("summary_json"):
            return parse_structured_summary(m.meta["summary_json"])
    return None


# ----------------------------------------------------- controller / orchestration


def transcript_hash(s: Session) -> str:
    h = hashlib.sha256()
    for m in s.messages:
        h.update(m.id.encode())
        h.update(m.policy.value.encode())
        h.update(m.content[:64].encode())
    return h.hexdigest()[:16]


class AutoCompactController:
    """Holds per-session auto-compaction state in s.meta['auto_compaction']."""

    def __init__(self, session: Session, cfg: AutoCompactConfig) -> None:
        self.s = session
        self.cfg = cfg
        self._meta = session.meta.setdefault("auto_compaction", {})
        self._meta.setdefault("state", "IDLE")
        self.machine = StateMachine(self._meta["state"])

    # --------------------------- persistence helpers
    def _persist(self) -> None:
        self._meta["state"] = self.machine.state

    def set_disabled(self, disabled: bool) -> None:
        self._meta["disabled_session"] = disabled
        self._persist()

    def reset_cooldown(self) -> None:
        for k in ("cooldown_until", "last_compaction_msg_count", "cooldown_turns_left",
                  "postpone", "was_postponed", "force_warned"):
            self._meta.pop(k, None)
        self._meta["failed_attempts"] = 0
        self._meta["armed"] = True
        self._persist()

    def _history(self, outcome: str, state_before: str | None = None, **extra: Any) -> None:
        """Lightweight audit trail for outcomes that do not produce a compaction entry."""
        hist = self._meta.setdefault("history", [])
        hist.append({"at": _now_iso(), "outcome": outcome, "state_before": state_before,
                     "state_after": self.machine.state, **extra})
        del hist[:-20]

    # --------------------------- cooldown / rearm
    def in_cooldown(self) -> bool:
        last = self._meta.get("last_compaction_msg_count", 0)
        turns = len(self.s.messages) - last
        return turns < self.cfg.cooldown_turns

    def cooldown_turns_left(self) -> int:
        last = int(self._meta.get("last_compaction_msg_count", 0))
        if not last:
            return 0
        return max(0, self.cfg.cooldown_turns - (len(self.s.messages) - last))

    def pressure_percent(self, expected_tool_tokens: int = 0) -> float:
        current = session_tokens(self.s.visible())
        overhead = _system_overhead(self.s)
        p = (current + overhead + self.cfg.reserve_output_tokens + expected_tool_tokens) / max(1, self.cfg.context_window)
        return p

    def _update_armed(self, pressure: float) -> None:
        """Hysteresis: after a success stay quiet until pressure dips below rearm_below_percent."""
        if pressure < self.cfg.rearm_below_percent / 100:
            self._meta["armed"] = True

    def _blocked_by_hysteresis(self, pressure: float) -> bool:
        # Escape hatch: at/above the recommend threshold we never stay disarmed forever.
        return (not self._meta.get("armed", True)) and pressure < self.cfg.recommend_at_percent / 100

    def should_evaluate(self, phase: dict | None = None) -> bool:
        if self._meta.get("disabled_session") or not self.cfg.enabled:
            return False
        pressure = self.pressure_percent()
        self._update_armed(pressure)
        if pressure >= self.cfg.force_at_percent / 100:
            if self.machine.state not in TERMINALISH and self.machine.state != "EMERGENCY":
                self.machine.try_transition("EMERGENCY") or self._force_emergency()
            self._persist()
            return True
        if pressure < self.cfg.evaluate_at_percent / 100:
            if self.machine.state == "IDLE":
                self.machine.try_transition("WATCHING")
            return False
        if self._blocked_by_hysteresis(pressure) or self.in_cooldown():
            return False
        if self.machine.state == "WATCHING":
            self.machine.try_transition("ELIGIBLE")
        self._persist()
        return True

    def _force_emergency(self) -> None:
        while self.machine.state not in ("EMERGENCY",):
            try:
                # find any transition to EMERGENCY by forcing intermediate states
                target = next((t for t in TRANSITIONS.get(self.machine.state, set()) if t == "EMERGENCY"), None)
                if target:
                    self.machine.transition(target)
                else:
                    break
            except ValueError:
                break

    def note_success(self, source_hash: str, msg_count: int) -> None:
        self._meta["last_success_hash"] = source_hash
        self._meta["last_compaction_msg_count"] = msg_count
        self._meta["failed_attempts"] = 0
        self._meta["cooldown_turns_left"] = self.cfg.cooldown_turns  # informational; see cooldown_turns_left()
        self._meta["armed"] = False
        for k in ("postpone", "was_postponed", "force_warned"):
            self._meta.pop(k, None)
        self.machine.state = "COOLDOWN"
        self._persist()

    def note_failure(self, source_hash: str, reason: str) -> None:
        self._meta["failed_attempts"] = int(self._meta.get("failed_attempts", 0)) + 1
        self._meta["last_fail_hash"] = source_hash
        self._meta["last_fail_reason"] = reason
        self._persist()

    def circuit_open(self, source_hash: str) -> tuple[bool, str]:
        if int(self._meta.get("failed_attempts", 0)) >= self.cfg.max_attempts:
            return True, f"max attempts ({self.cfg.max_attempts}) reached"
        if self._meta.get("last_fail_hash") == source_hash and self._meta.get("failed_attempts", 0) >= 2:
            return True, "same source transcript already failed twice"
        return False, ""

    # --------------------------- rollback
    def rollback(self) -> bool:
        """Restore the master transcript from the snapshot taken before the last automatic apply."""
        snap = self._meta.get("rollback")
        if not snap:
            return False
        self.s.messages = [Message.from_dict(d) for d in snap["messages"]]
        comps = self.s.meta.get("compactions", [])
        if comps and comps[-1].get("automatic"):
            comps.pop()
        self._meta.pop("rollback", None)
        self.machine.state = "WATCHING"
        self.reset_cooldown()
        self._history("rolled_back", state_before="APPLIED")
        return True

    # --------------------------- main evaluation entry
    def evaluate(self, keep_recent: int | None = None, scopes=None, checkpoint: dict | None = None,
                 ui: "PromptUI | None" = None, apply_fn: Callable[[Session], Session] | None = None,
                 interactive: bool = True) -> dict:
        """Run one evaluation cycle. Returns a result dict describing what happened."""
        cfg = self.cfg
        if keep_recent is None:
            keep_recent = cfg.keep_recent
        src_hash = transcript_hash(self.s)
        open_pair = bool(_open_pair_ids(self.s))
        result: dict[str, Any] = {"action": "none", "state": self.machine.state}

        if self._meta.get("disabled_session") or not cfg.enabled:
            result["action"] = "disabled"
            return result

        pressure = self.pressure_percent(cfg.reserve_tool_tokens if open_pair else 0)
        self._update_armed(pressure)
        if pressure < cfg.evaluate_at_percent / 100 and pressure < cfg.force_at_percent / 100:
            result["action"] = "below_threshold"
            result["pressure"] = pressure
            return result

        # hysteresis / cooldown
        if self._blocked_by_hysteresis(pressure) and pressure < cfg.force_at_percent / 100:
            result["action"] = "cooldown"
            return result
        if self.in_cooldown() and pressure < cfg.force_at_percent / 100:
            result["action"] = "cooldown"
            return result

        po = self._meta.get("postpone")
        if po and pressure < cfg.force_at_percent / 100:
            if po["type"] == "turns" and len(self.s.messages) - po.get("at", 0) < po["amount"]:
                result.update(action="postponed_turns")
                return result
            if po["type"] == "tokens" and session_tokens(self.s.messages[po.get("at", 0):]) < po["amount"]:
                result.update(action="postponed_tokens")
                return result
            self._meta.pop("postpone", None)

        # minimum accumulation gates
        last_count = int(self._meta.get("last_compaction_msg_count", 0))
        turns_since = len(self.s.messages) - last_count
        tokens_since = session_tokens(self.s.messages[last_count:])
        if turns_since < cfg.min_turns_since_compaction and last_count > 0:
            result["action"] = "cooldown_turns"
            return result
        if tokens_since < cfg.min_new_tokens and last_count > 0:
            result["action"] = "cooldown_tokens"
            return result

        # emergency short-circuit
        is_emergency = pressure >= cfg.force_at_percent / 100
        if is_emergency and not is_safe_boundary(self.s, checkpoint):
            self.machine.try_transition("PENDING_BOUNDARY") or self.machine.try_transition("ELIGIBLE")
            self._meta["compaction_pending"] = True
            self._persist()
            result.update(action="pending_boundary", pressure=pressure)
            return result

        if not is_safe_boundary(self.s, checkpoint):
            self.machine.try_transition("ELIGIBLE")
            self.machine.try_transition("PENDING_BOUNDARY")
            self._meta["compaction_pending"] = True
            self._persist()
            result.update(action="pending_boundary", pressure=pressure)
            return result

        if self.machine.state == "REJECTED":
            self.machine.try_transition("WATCHING")
        if is_emergency:
            self.machine.state = "EMERGENCY"
        elif self.machine.state in ("IDLE", "WATCHING"):
            self.machine.try_transition("ELIGIBLE")
        if not is_emergency:
            if self.machine.state == "ELIGIBLE":
                self.machine.try_transition("PENDING_BOUNDARY")
            if self.machine.state == "PENDING_BOUNDARY":
                self.machine.try_transition("BUILDING_CANDIDATE")
        state_before = self.machine.state
        self._persist()

        blocked, why = self.circuit_open(src_hash)
        assessment = assess(self.s, cfg, keep_recent=keep_recent, scopes=scopes, checkpoint=checkpoint)
        result["assessment"] = asdict(assessment)
        result["pressure"] = pressure

        if blocked:
            self.machine.try_transition("REJECTED")
            self._persist()
            self._history("circuit_breaker", state_before, reason=why)
            result.update(action="circuit_breaker", reason=why)
            return result

        if assessment.recommendation == "skip" or (assessment.blockers and "no safe boundary" not in assessment.blockers[0]):
            self.machine.try_transition("REJECTED")
            self._persist()
            result.update(action="skipped", reason=assessment.blockers[0] if assessment.blockers else "low value")
            return result

        # continuity checkpoint: caller-supplied fields override the captured ones
        ckpt = {**capture_checkpoint(self.s), **(checkpoint or {})}
        result["checkpoint_id"] = checkpoint_id(ckpt)

        # user interaction decision
        user_action: str | None = None
        ask_user = self._should_ask(assessment, cfg, interactive) and not is_emergency
        essence_directives = pending_essences(self.s)

        if (is_emergency and interactive and ui is not None and self._meta.get("was_postponed")
                and not self._meta.get("force_warned")):
            _say(ui, f"codec: context is {int(pressure * 100)}% full - compaction was postponed; compacting now.")
            self._meta["force_warned"] = True

        def candidate_fn() -> Session | None:
            return apply_fn(self.s) if apply_fn is not None else None

        if ask_user and ui is not None and interactive:
            self.machine.try_transition("WAITING_FOR_USER")
            self._persist()
            verdict, essence_directives = self._prompt_user(
                ui, assessment, essence_directives, keep_recent=keep_recent, scopes=scopes,
                candidate_fn=candidate_fn)
            if verdict == "default":
                verdict = {"postpone": "later", "skip": "skip", "apply": "apply"}[cfg.default_action]
            if verdict == "skip":
                self.machine.try_transition("REJECTED")
                self._persist()
                self._history("skipped", state_before, user_action="skip")
                result.update(action="skipped", reason="default_action=skip")
                return result
            if verdict == "later":
                if "postpone" not in self._meta:
                    self._meta["postpone"] = {"type": "turns", "amount": self._postpone_turns(ui),
                                              "at": len(self.s.messages)}
                self._meta["was_postponed"] = True
                self.machine.try_transition("REJECTED")
                self._persist()
                self._history("later", state_before, user_action="later")
                result.update(action="postponed")
                return result
            if verdict == "disabled":
                self.set_disabled(True)
                self._history("disabled", state_before, user_action="disabled")
                result.update(action="disabled_for_session")
                return result
            if verdict == "review":
                result.update(action="review_requested", plan=self._plan_summary(keep_recent, scopes))
                return result
            # apply / added_essence both flow through to candidate build
            user_action = "added_essence" if verdict == "added_essence" else "apply"
            result["user_action"] = user_action
        elif ask_user and not interactive:
            action = "apply-if-valid" if is_emergency else cfg.noninteractive_action
            if action == "disable":
                self.set_disabled(True)
                self._history("disabled", state_before, user_action=None)
                result.update(action="disabled_for_session")
                return result
            if action == "postpone":
                self._meta["was_postponed"] = True
                result.update(action="postponed_noninteractive")
                return result
            # apply-if-valid falls through

        # build candidate
        if apply_fn is None:
            result.update(action="no_compactor")
            return result
        self.machine.try_transition("VERIFYING")
        self._persist()
        candidate = apply_fn(self.s)
        candidate.messages = insert_checkpoint(candidate.messages, ckpt)
        facts = collect_exact_facts(self.s)
        p = plan(self.s, keep_recent=keep_recent, scopes=scopes or [])
        reduction = 1 - session_tokens(candidate.visible()) / max(1, session_tokens(self.s.visible()))
        ver = verify(self.s, candidate, essence_directives, cfg, reduction, checkpoint=ckpt, exact_facts=facts)
        result["verification"] = {"passed": ver.passed, "errors": ver.errors, "warnings": ver.warnings, "repair_attempted": ver.repair_attempted}

        if not ver.passed:
            self.machine.try_transition("REPAIRING")
            self._persist()
            repaired = repair(candidate, self.s, essence_directives, checkpoint=ckpt)
            reduction2 = 1 - session_tokens(repaired.visible()) / max(1, session_tokens(self.s.visible()))
            ver2 = verify(self.s, repaired, essence_directives, cfg, reduction2, checkpoint=ckpt, exact_facts=facts)
            result["verification"] = {"passed": ver2.passed, "errors": ver2.errors, "warnings": ver2.warnings, "repair_attempted": True}
            if ver2.passed:
                candidate = repaired
                ver = ver2
                ver.repair_attempted = True
            else:
                self.machine.try_transition("REJECTED")
                self.note_failure(src_hash, _safe_reason(ver2.errors))
                self._persist()
                self._history("rejected", state_before, user_action=user_action, reason=_safe_reason(ver2.errors))
                result.update(action="rejected", reason="; ".join(ver2.errors[:3]))
                return result

        before_apply = self.machine.state
        self.machine.try_transition("APPLIED")
        self._persist()
        audit = {
            "at": _now_iso(),
            "automatic": True,
            "trigger_percent": int(pressure * 100),
            "effective_pressure": round(pressure, 3),
            "assessment": {"projected_reduction": assessment.projected_reduction,
                           "semantic_risk": assessment.semantic_risk,
                           "recommendation": assessment.recommendation,
                           "reasons": assessment.reasons[:5]},
            "user_action": user_action,
            "essence_directive_ids": [d.get("id", "") for d in essence_directives],
            "continuity_checkpoint_id": result["checkpoint_id"],
            "verification": {"passed": True, "repair_attempted": ver.repair_attempted, "errors": [], "warnings": ver.warnings},
            "state_before": before_apply, "state_after": "APPLIED",
            "summarized": len(p.to_summarize), "dropped": len(p.dropped), "excluded": 0,
            "tokens_before": session_tokens(self.s.visible()),
            "tokens_after": session_tokens(candidate.visible()),
        }
        old_messages = self.s.messages
        if cfg.rollback_on_failure:
            self._meta["rollback"] = {"messages": [m.to_dict() for m in old_messages],
                                      "source_hash": src_hash, "at": _now_iso()}
        try:
            self.s.messages = candidate.messages
            self.s.agent = candidate.agent
            self.s.project = candidate.project
            self.s.meta.setdefault("compactions", []).append(audit)
        except Exception:
            self.s.messages = old_messages
            raise
        expire_next_compaction(self.s)
        self.note_success(src_hash, len(self.s.messages))
        self._history("applied", before_apply, user_action=user_action)
        result.update(action="applied", state=self.machine.state)
        return result

    # --------------------------- helpers
    def _should_ask(self, assessment: CompactionAssessment, cfg: AutoCompactConfig, interactive: bool) -> bool:
        if cfg.ask_user == "always":
            return True
        if cfg.ask_user == "never":
            return False
        if cfg.ask_user == "critical-only":
            return assessment.semantic_risk in ("high", "critical") and self.pressure_percent() >= cfg.force_at_percent / 100
        # smart
        ask = assessment.semantic_risk in ("medium", "high", "critical")
        ask = ask or bool(assessment.blockers)
        ask = ask or assessment.recommendation == "ask"
        return ask

    def _preserved_counts(self) -> str:
        msgs = self.s.visible()
        n = lambda tag: sum(1 for m in msgs if tag in m.tags)  # noqa: E731
        return (f"Guaranteed preserved:\n  {n('goal')} goals\n  {n('constraint')} constraints\n"
                f"  {n('decision')} decisions\n  current phase and next action\n"
                f"  pinned messages and exact facts\n")

    def _prompt_user(self, ui: "PromptUI", assessment: CompactionAssessment, essences: list[dict],
                     keep_recent: int = 6, scopes=None,
                     candidate_fn: Callable[[], Session | None] | None = None) -> tuple[str, list[dict]]:
        cached: dict[str, Session | None] = {}
        menu = (
            f"codec: context is {int(assessment.effective_pressure * 100)}% full.\n\n"
            f"Compaction recommendation:\n"
            f"  Current context:     {assessment.current_tokens:,} tokens\n"
            f"  Projected context:   {assessment.projected_output_tokens:,} tokens\n"
            f"  Projected reduction: {int(assessment.projected_reduction * 100)}%\n"
            f"  Semantic risk:       {assessment.semantic_risk.upper()}\n"
            + (f"  Main reason: {assessment.reasons[0]}\n" if assessment.reasons else "")
            + "\n" + self._preserved_counts() + "\n"
            "Plan:\n  Keep verbatim:  pinned + recent\n  Summarize:      middle turns\n  Drop:           disposable tool output\n\n"
            "[A] Apply\n[E] Add essence\n[R] Review plan\n[L] Later\n[N] Disable for this session\n> "
        )
        while True:
            choice = ui.ask(menu).strip().lower()
            if choice == "r":
                self._review_loop(ui, assessment, keep_recent, scopes, candidate_fn, cached)
                continue
            break
        if choice == "e":
            directive_text = ui.ask("What must survive this compaction?\n> ").strip()
            if directive_text:
                add_essence(self.s, directive_text)
                essences = pending_essences(self.s)
            confirm = ui.ask(f'Essence recorded: "{directive_text}". [A] Apply / [L] Later\n> ').strip().lower()
            if confirm.startswith("a"):
                return "added_essence", essences
            return "later", essences
        if choice == "l" or choice.startswith("l "):
            rest = choice[1:].strip()
            if rest.endswith("t") and rest[:-1].isdigit():
                self._meta["postpone"] = {"type": "tokens", "amount": int(rest[:-1]), "at": len(self.s.messages)}
            elif rest.isdigit():
                self._meta["postpone"] = {"type": "turns", "amount": int(rest), "at": len(self.s.messages)}
            self._persist()
            return "later", essences
        if choice == "n":
            return "disabled", essences
        if choice == "a":
            return "apply", essences
        if not choice:
            return "default", essences
        # conservative parse of "keep ...; apply": anything else is treated as an essence directive
        directive, action = parse_essence_input(choice)
        if directive:
            add_essence(self.s, directive)
            essences = pending_essences(self.s)
            if action == "apply":
                return "added_essence", essences
            confirm = ui.ask(f'Treated as essence: "{directive}". [A] Apply / [L] Later\n> ').strip().lower()
            return ("added_essence" if confirm.startswith("a") else "later"), essences
        return "default", essences

    def _review_loop(self, ui: "PromptUI", assessment: CompactionAssessment, keep_recent: int, scopes,
                     candidate_fn, cached: dict) -> None:
        p = plan(self.s, keep_recent=keep_recent, scopes=scopes or [])
        while True:
            sub = ui.ask("[S] Summary  [I] Items  [D] Structured diff  [B] Back\n> ").strip().lower()
            if sub == "s":
                _say(ui, review_summary(p, assessment.semantic_risk))
            elif sub == "i":
                _say(ui, "\n".join(review_items(self.s, p)))
            elif sub == "d":
                if "c" not in cached:
                    try:
                        cached["c"] = candidate_fn() if candidate_fn else None
                    except Exception as e:  # LLM/network failure must not break the prompt
                        cached["c"] = None
                        _say(ui, f"(could not build candidate: {e})")
                cand = cached["c"]
                new = _summary_high_of(cand) if cand is not None else None
                _say(ui, review_diff(_summary_high_of(self.s), new))
            else:  # b, empty (EOF) or anything else
                return

    def _postpone_turns(self, ui: "PromptUI") -> int:
        return max(1, self.cfg.cooldown_turns)

    def _plan_summary(self, keep_recent, scopes) -> str:
        return review_summary(plan(self.s, keep_recent=keep_recent, scopes=scopes or []))


class PromptUI:
    """Injectable terminal prompt. Default implementation reads stdin."""

    def __init__(self, input_fn: Callable[[str], str] | None = None, output_fn: Callable[[str], None] | None = None):
        self._in = input_fn or input
        self._out = output_fn or (lambda t: print(t, end="", flush=True))

    def ask(self, text: str) -> str:
        self._out(text)
        try:
            return self._in("")
        except EOFError:
            return ""

    def info(self, text: str) -> None:
        self._out(text + "\n")

"""Core data structures: Policy, Message, Session. Agent-agnostic."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# Tags that force verbatim retention during compaction.
VERBATIM_TAGS = frozenset({"goal", "constraint"})


@dataclass
class Rule:
    """Optional per-message / per-scope knobs beyond the base Policy."""

    keep_verbatim_until: int | None = None   # pinned for this many compactions
    drop_ok_after: int | None = None         # only droppable once this old (turns)
    summarize_detail: str | None = None      # high | medium | low (None = medium)
    truncate_result_to: int | None = None    # keep first/last N tokens of result
    keep_result_sections: list[str] | None = None
    drop_result_if: str | None = None        # no_errors | always | never

    def merged_with(self, override: "Rule | None") -> "Rule":
        if override is None:
            return self
        base = self.__dict__.copy()
        for k, v in override.__dict__.items():
            if v is not None:
                base[k] = v
        return Rule(**base)

    def detail(self) -> str:
        return self.summarize_detail or "medium"

    def strictness(self) -> tuple[int, int, int, int, int]:
        """Higher tuple = stricter. Compared field-wise for tie-breaks."""
        detail = {"high": 2, "medium": 1, "low": 0, None: 1}[self.summarize_detail]
        drop = {"never": 2, "no_errors": 1, "always": 0, None: -1}[self.drop_result_if]
        return (
            detail,
            drop,
            self.truncate_result_to if self.truncate_result_to is not None else -1,
            self.keep_verbatim_until if self.keep_verbatim_until is not None else -1,
            self.drop_ok_after if self.drop_ok_after is not None else -1,
        )

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Rule | None":
        if not d:
            return None
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Policy(str, Enum):
    KEEP_VERBATIM = "keep_verbatim"  # never summarized or dropped
    SUMMARIZE_OK = "summarize_ok"    # default: may be folded into the summary
    DROP_OK = "drop_ok"              # may be discarded when compacting
    DROP_ALWAYS = "drop_always"      # never enters agent-visible context


@dataclass
class Message:
    role: str                        # user | assistant | tool
    content: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    tags: set[str] = field(default_factory=set)
    policy: Policy = Policy.SUMMARIZE_OK
    timestamp: str = field(default_factory=_now)
    meta: dict[str, Any] = field(default_factory=dict)  # adapter-private data
    rule: Rule | None = None

    @property
    def is_pinned(self) -> bool:
        """True if compaction must keep this message verbatim."""
        return self.policy is Policy.KEEP_VERBATIM or bool(self.tags & VERBATIM_TAGS)

    def pair_ids(self) -> set[str]:
        """Tool-call identifiers linking this message to its tool_use/tool_result partner."""
        ids = self.meta.get("pair_ids") or []
        if single := self.meta.get("pair_id"):
            ids = [*ids, single]
        return set(ids)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id, "role": self.role, "content": self.content,
            "tags": sorted(self.tags), "policy": self.policy.value,
            "timestamp": self.timestamp, "meta": self.meta,
        }
        if self.rule is not None:
            d["rule"] = self.rule.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        return cls(
            role=d["role"], content=d["content"], id=d.get("id") or uuid.uuid4().hex[:8],
            tags=set(d.get("tags", [])), policy=Policy(d.get("policy", "summarize_ok")),
            timestamp=d.get("timestamp") or _now(), meta=d.get("meta", {}),
            rule=Rule.from_dict(d.get("rule")),
        )


@dataclass
class Session:
    agent: str
    project: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=_now)
    messages: list[Message] = field(default_factory=list)
    system: str = ""                                  # system prompt, kept as-is
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def visible(self) -> list[Message]:
        """Messages the agent is allowed to see (drop_always removed)."""
        return [m for m in self.messages if m.policy is not Policy.DROP_ALWAYS]

    def resolve(self, target: str) -> Message:
        """Resolve 'last', a 0-based/negative index, or an id prefix to a Message."""
        if not self.messages:
            raise LookupError("session is empty")
        if target == "last":
            return self.messages[-1]
        try:
            return self.messages[int(target)]
        except (ValueError, IndexError):
            pass
        hits = [m for m in self.messages if m.id.startswith(target)]
        if len(hits) != 1:
            raise LookupError(f"target {target!r} matched {len(hits)} messages")
        return hits[0]

    def header(self) -> dict[str, Any]:
        return {
            "schema": 1, "id": self.id, "agent": self.agent, "project": self.project,
            "created_at": self.created_at, "system": self.system,
            "tool_schemas": self.tool_schemas, "meta": self.meta,
        }

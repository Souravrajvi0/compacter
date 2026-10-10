"""Structured, layered summary schema + validation + formatting."""
from __future__ import annotations

import json
import re
from typing import Any

from .tokens import estimate_tokens

SCHEMA_EXAMPLE = """{
  "summary_high": {
    "goals": [], "constraints": [],
    "decisions": [{"what": "", "why": "", "alternatives_rejected": []}],
    "open_tasks": [], "key_files": []
  },
  "summary_medium": "",
  "summary_low": ""
}"""

_HIGH_KEYS = ("goals", "constraints", "decisions", "open_tasks", "key_files")


def parse_structured_summary(text: str) -> dict[str, Any] | None:
    """Return the structured summary dict, or None if text isn't valid."""
    cleaned = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if m:
        cleaned = m.group(1).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or "summary_high" not in data:
        return None
    high = data["summary_high"]
    if not isinstance(high, dict) or not all(isinstance(high.get(k), list) for k in _HIGH_KEYS):
        return None
    if not all(isinstance(data.get(k), str) for k in ("summary_medium", "summary_low")):
        return None
    for d in high["decisions"]:
        if not isinstance(d, dict) or "what" not in d:
            return None
    return data


def _format_high(high: dict[str, Any]) -> str:
    lines: list[str] = []
    if high["goals"]:
        lines.append("## Goals")
        lines += [f"- {g}" for g in high["goals"]]
    if high["constraints"]:
        lines.append("## Constraints")
        lines += [f"- {c}" for c in high["constraints"]]
    if high["decisions"]:
        lines.append("## Decisions")
        for d in high["decisions"]:
            why = f" (why: {d['why']})" if d.get("why") else ""
            lines.append(f"- {d['what']}{why}")
    if high["open_tasks"]:
        lines.append("## Open tasks")
        lines += [f"- {t}" for t in high["open_tasks"]]
    if high["key_files"]:
        lines.append("## Key files")
        lines += [f"- {f}" for f in high["key_files"]]
    return "\n".join(lines) or "(no high-level state recorded)"


def format_summary_block(data: dict[str, Any], token_budget: int = 1500) -> str:
    """Assemble the layered summary as one text block.

    summary_high is always included. summary_medium is included if the pair
    fits in token_budget. summary_low only if everything fits.
    """
    high_text = _format_high(data["summary_high"])
    block = high_text
    if data["summary_medium"] and estimate_tokens(high_text) + estimate_tokens(data["summary_medium"]) <= token_budget:
        block += "\n\n## Reasoning\n" + data["summary_medium"]
    if data["summary_low"] and estimate_tokens(block) + estimate_tokens(data["summary_low"]) <= token_budget:
        block += "\n\n## Exploratory notes\n" + data["summary_low"]
    return block

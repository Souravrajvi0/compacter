"""Context packs: a saved compaction you can carry into any new chat.

A pack holds the latest compaction summary plus the messages you pinned (goal / constraint /
keep_verbatim). It lives in ~/.ctxpipe/packs (override with CTXPIPE_HOME), so it is usable from any
project and any agent. Seeding a chat writes the pack as the first message of a brand-new session.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Message, Session
from .prompts import SUMMARY_HEADER
from .summary import format_summary_block, parse_structured_summary

SUMMARY_TAG = "compaction_summary"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ACK = "Understood. I have the context carried over from the earlier work and will continue from it."


def pack_dir() -> Path:
    return Path(os.environ.get("CTXPIPE_HOME") or Path.home() / ".ctxpipe") / "packs"


def _path(name: str) -> Path:
    if not NAME_RE.match(name):
        raise ValueError(f"invalid pack name {name!r} (letters, digits, . _ - ; max 64)")
    return pack_dir() / f"{name}.json"


def _latest_summary(session: Session) -> Message | None:
    found = [m for m in session.messages if SUMMARY_TAG in m.tags or m.content.startswith(SUMMARY_HEADER)]
    return found[-1] if found else None


def save_pack(name: str, session: Session, note: str = "") -> Path:
    """Save the session's latest compaction summary + pinned messages as a pack."""
    summary = _latest_summary(session)
    if summary is None:
        raise ValueError("this session has no compaction yet; run `ctxpipe compact --apply` first")
    pinned = [
        {"role": m.role, "content": m.content, "tags": sorted(m.tags)}
        for m in session.messages
        if m.is_pinned and m is not summary and SUMMARY_TAG not in m.tags
    ]
    body = summary.content[len(SUMMARY_HEADER):] if summary.content.startswith(SUMMARY_HEADER) else summary.content
    return _write(name, {
        "note": note, "source": {"agent": session.agent, "project": session.project},
        "summary_text": body, "summary_json": summary.meta.get("summary_json"), "pinned": pinned,
    })


def carry_pack(name: str, session: Session, chosen: list[Message], summary: str | None = None,
               note: str = "") -> Path:
    """A pack made from messages the user picked.

    summary=None carries the chosen messages verbatim; otherwise `summary` (the LLM's structured JSON,
    or free text) stands in for them. The source chat is never modified.
    """
    if not chosen:
        raise ValueError("no messages selected")
    data = parse_structured_summary(summary) if summary else None
    pack: dict[str, Any] = {
        "note": note, "source": {"agent": session.agent, "project": session.project},
        "summary_text": "" if data else (summary or ""),
        "summary_json": summary if data else None,
        "pinned": [] if summary else [{"role": m.role, "content": m.content, "tags": sorted(m.tags)} for m in chosen],
        "carried": len(chosen),
    }
    return _write(name, pack)


def _write(name: str, pack: dict[str, Any]) -> Path:
    pack = {"schema": 1, "name": name, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **pack}
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(pack, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_pack(name: str) -> dict[str, Any]:
    path = _path(name)
    if not path.exists():
        raise FileNotFoundError(f"no pack named {name!r} (see `ctxpipe pack list`)")
    return json.loads(path.read_text(encoding="utf-8"))


def list_packs() -> list[dict[str, Any]]:
    out = []
    for p in sorted(pack_dir().glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        out.append({"name": d.get("name", p.stem), "created_at": d.get("created_at", ""),
                    "agent": d.get("source", {}).get("agent", ""), "note": d.get("note", ""),
                    "pinned": len(d.get("pinned", []))})
    return out


def delete_pack(name: str) -> bool:
    path = _path(name)
    if path.exists():
        path.unlink()
        return True
    return False


def render_markdown(pack: dict[str, Any]) -> str:
    """The pack as one block of text: summary first, then the pinned messages."""
    data = parse_structured_summary(pack["summary_json"]) if pack.get("summary_json") else None
    summary = format_summary_block(data, 4000) if data else pack.get("summary_text", "")
    parts = [f"[ctxpipe context pack '{pack['name']}': carried over from an earlier chat]", summary]
    if pack.get("pinned"):
        parts.append("## Messages carried over from the earlier chat (verbatim)")
        parts += [f"- ({p['role']}) {p['content']}" for p in pack["pinned"]]
    return "\n\n".join(x for x in parts if x)


def seed_session(pack: dict[str, Any], agent: str, project: Path) -> Session:
    """A brand-new session whose first message is the pack (chain-friendly: keeps summary_json)."""
    meta = {"summary_json": pack["summary_json"]} if pack.get("summary_json") else {}
    first = Message(role="user", content=render_markdown(pack), tags={SUMMARY_TAG}, meta=meta)
    reply = Message(role="assistant", content=ACK, tags={"synthetic"})
    return Session(agent=agent, project=str(project.resolve()), messages=[first, reply],
                   meta={"seeded_from_pack": pack["name"]})

"""Claude Code adapter (experimental).

Claude Code stores sessions as JSONL at ~/.claude/projects/<encoded-project-path>/<uuid>.jsonl.
Each line is an event; user/assistant events carry `message.{role,content}`, `uuid` and
`parentUuid`. This adapter maps text content only; tool_use/tool_result blocks are flattened
to short text so they can be filtered and summarized. Writes go to a NEW session file
(never overwriting the original) which can be resumed with `claude --resume <id>`.

Schema verified against real Claude Code sessions (user/assistant events carry
`uuid`, `parentUuid`, `message.role`, `message.content`); non-message event types
(attachment, file-history-*, etc.) are ignored. Re-check after major Claude Code upgrades.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.models import Message, Session
from .base import Adapter, register

CLAUDE_HOME = Path.home() / ".claude" / "projects"


def encode_project_dir(project: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(project.resolve()))


def _flatten(content: Any) -> tuple[str, list[str]]:
    if isinstance(content, str):
        return content, []
    parts: list[str] = []
    pair_ids: list[str] = []
    for block in content or []:
        t = block.get("type")
        if t == "text":
            parts.append(block.get("text", ""))
        elif t == "tool_use":
            parts.append(f"[tool_use {block.get('name')}: {json.dumps(block.get('input'))[:500]}]")
            if block.get("id"):
                pair_ids.append(block["id"])
        elif t == "tool_result":
            parts.append(f"[tool_result: {_flatten(block.get('content'))[0][:2000]}]")
            if block.get("tool_use_id"):
                pair_ids.append(block["tool_use_id"])
    return "\n".join(p for p in parts if p), pair_ids


@register
class ClaudeCodeAdapter(Adapter):
    name = "claude-code"

    def _session_dir(self, project: Path) -> Path:
        return CLAUDE_HOME / encode_project_dir(project)

    def latest_session_file(self, project: Path) -> Path:
        files = sorted(self._session_dir(project).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not files:
            raise FileNotFoundError(f"no Claude Code sessions found for {project}")
        return files[-1]

    def read(self, project: Path) -> Session:
        src = self.latest_session_file(project)
        session = Session(agent=self.name, project=str(project.resolve()),
                          meta={"source_file": str(src)})
        for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") not in ("user", "assistant") or "message" not in ev:
                continue
            text, pair_ids = _flatten(ev["message"].get("content"))
            if not text.strip():
                continue
            meta: dict[str, Any] = {"uuid": ev.get("uuid")}
            if pair_ids:
                meta["pair_ids"] = pair_ids
            session.messages.append(Message(
                role=ev["message"]["role"], content=text,
                timestamp=ev.get("timestamp", ""), meta=meta,
            ))
        return session

    def write(self, session: Session, project: Path) -> Path:
        sid = str(uuid.uuid4())
        out = self._session_dir(project) / f"{sid}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        parent: str | None = None
        lines = []
        for m in session.messages:
            ev_id = str(uuid.uuid4())
            lines.append(json.dumps({
                "type": m.role, "uuid": ev_id, "parentUuid": parent, "sessionId": sid,
                "cwd": session.project, "isSidechain": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {"role": m.role, "content": [{"type": "text", "text": m.content}]},
            }))
            parent = ev_id
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return out

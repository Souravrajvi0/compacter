"""Codex adapter. Sessions live at ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.

Lines: {timestamp, type, payload}. type "session_meta" carries payload.cwd;
"response_item" carries payload {type: message|function_call|function_call_output}.
Writes a NEW rollout file (original untouched).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.models import Message, Session
from .base import Adapter, register

CODEX_HOME = Path.home() / ".codex" / "sessions"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        t = block.get("type", "")
        if t in ("input_text", "output_text", "text"):
            parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p)


@register
class CodexAdapter(Adapter):
    name = "codex"

    def _iter_files(self) -> list[Path]:
        return sorted(CODEX_HOME.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime)

    def latest_session_file(self, project: Path) -> Path:
        want = os.path.normcase(str(project.resolve()))
        for f in reversed(self._iter_files()):
            try:
                first = json.loads(f.read_text(encoding="utf-8", errors="replace").splitlines()[0])
            except (IndexError, json.JSONDecodeError, UnicodeError):
                continue
            if (first.get("type") == "session_meta"
                    and os.path.normcase(str(first["payload"].get("cwd", ""))) == want):
                return f
        raise FileNotFoundError(f"no Codex sessions found for {project}")

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
            if ev.get("type") != "response_item":
                continue
            p = ev.get("payload", {})
            pt = p.get("type")
            if pt == "message" and p.get("role") in ("user", "assistant"):
                text = _text_of(p.get("content"))
                if text.strip():
                    session.messages.append(Message(role=p["role"], content=text,
                                                    timestamp=ev.get("timestamp", "")))
            elif pt == "function_call":
                session.messages.append(Message(
                    role="assistant",
                    content=f"[tool_use {p.get('name')}: {str(p.get('arguments', ''))[:500]}]",
                    timestamp=ev.get("timestamp", ""),
                    meta={"pair_ids": [p["call_id"]]} if p.get("call_id") else {},
                ))
            elif pt == "function_call_output":
                session.messages.append(Message(
                    role="tool",
                    content=f"[tool_result: {str(p.get('output', ''))[:2000]}]",
                    timestamp=ev.get("timestamp", ""),
                    meta={"pair_ids": [p["call_id"]]} if p.get("call_id") else {},
                ))
        return session

    def write(self, session: Session, project: Path) -> Path:
        out_dir = CODEX_HOME / datetime.now(timezone.utc).strftime("%Y/%m/%d")
        out_dir.mkdir(parents=True, exist_ok=True)
        sid = str(uuid.uuid4())
        out = out_dir / f"rollout-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')}-{sid}.jsonl"
        lines = [json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "session_meta",
            "payload": {"session_id": sid, "id": sid, "cwd": session.project, "originator": "compacter"},
        })]
        for m in session.messages:
            if m.role == "tool":
                continue
            block_type = "input_text" if m.role == "user" else "output_text"
            lines.append(json.dumps({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "response_item",
                "payload": {"type": "message", "role": m.role,
                            "content": [{"type": block_type, "text": m.content}]},
            }))
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return out

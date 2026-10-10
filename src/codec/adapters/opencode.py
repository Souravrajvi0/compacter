"""opencode adapter (experimental).

opencode stores sessions in a sqlite db at ~/.local/share/opencode/opencode.db
(on Windows: %USERPROFILE%\\.local\\share\\opencode\\opencode.db). Tables:
session(id, directory, title, ...), message(id, session_id, data), part(id,
message_id, session_id, data). message.data.role is "user"/"assistant"; parts
have types text/tool/reasoning/step-start/step-finish/compaction. Text parts
are mapped verbatim; tool parts are flattened like the other adapters.
Writes create a NEW session (new ses_/msg_/prt_ ids) — the original is untouched.
Resume with `opencode --session <new session id>`. Schema verified against
opencode's sqlite storage circa 2026-10; re-check after upgrades.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.models import Message, Session
from .base import Adapter, register

OPENCODE_HOME = Path.home() / ".local" / "share" / "opencode"
DB_FILE = OPENCODE_HOME / "opencode.db"


def _norm_dir(p: str) -> str:
    return os.path.normcase(str(p).replace("\\", "/").rstrip("/"))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _flatten_part(data: dict[str, Any]) -> tuple[str | None, list[str]]:
    t = data.get("type")
    if t == "text":
        text = data.get("text", "")
        return (text if text.strip() else None), []
    if t == "reasoning":
        text = data.get("text", "")
        return (f"[reasoning: {text[:500]}]" if text.strip() else None), []
    if t == "tool":
        state = data.get("state") or {}
        tool = data.get("tool", "?")
        inp = json.dumps(state.get("input", {}))[:500]
        out = (state.get("metadata") or {}).get("output", "") or state.get("output", "")
        parts = [f"[tool_use {tool}: {inp}]"]
        if out:
            parts.append(f"[tool_result: {str(out)[:2000]}]")
        pair = [data["callID"]] if data.get("callID") else []
        return "\n".join(parts), pair
    return None, []


@register
class OpencodeAdapter(Adapter):
    name = "opencode"

    def _db(self) -> sqlite3.Connection:
        if not DB_FILE.exists():
            raise FileNotFoundError(f"opencode db not found at {DB_FILE}")
        return sqlite3.connect(str(DB_FILE))

    def latest_session_id(self, project: Path) -> str:
        if explicit := os.environ.get("CODEC_OPENCODE_SESSION"):
            return explicit
        want = _norm_dir(str(project.resolve()))
        with self._db() as c:
            rows = c.execute(
                "select id, directory from session order by time_updated desc"
            ).fetchall()
        for sid, directory in rows:
            if _norm_dir(directory or "") == want:
                return sid
        raise FileNotFoundError(f"no opencode sessions found for {project}")

    def read(self, project: Path) -> Session:
        sid = self.latest_session_id(project)
        session = Session(agent=self.name, project=str(project.resolve()),
                          meta={"source_session": sid, "db": str(DB_FILE)})
        with self._db() as c:
            msgs = c.execute(
                "select id, data from message where session_id=? order by time_created", (sid,)
            ).fetchall()
            for mid, mdata in msgs:
                try:
                    m = json.loads(mdata)
                except json.JSONDecodeError:
                    continue
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                parts = c.execute(
                    "select data from part where message_id=? order by time_created", (mid,)
                ).fetchall()
                texts: list[str] = []
                pair_ids: list[str] = []
                for (pdata,) in parts:
                    try:
                        p = json.loads(pdata)
                    except json.JSONDecodeError:
                        continue
                    text, pairs = _flatten_part(p)
                    if text:
                        texts.append(text)
                    pair_ids.extend(pairs)
                content = "\n".join(texts)
                if not content.strip():
                    continue
                meta: dict[str, Any] = {"source_id": mid}
                if pair_ids:
                    meta["pair_ids"] = pair_ids
                created = ((m.get("time") or {}).get("created") or 0) / 1000
                session.messages.append(Message(
                    role=role, content=content,
                    timestamp=datetime.fromtimestamp(created, tz=timezone.utc).isoformat() if created else "",
                    meta=meta,
                ))
        return session

    def write(self, session: Session, project: Path) -> Path:
        sid = _new_id("ses")
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        with self._db() as c:
            # Template message data from an existing session in this project so
            # required fields (modelID, providerID, agent, mode, ...) carry over.
            try:
                src_sid = self.latest_session_id(project)
            except FileNotFoundError:
                src_sid = None
            template: dict[str, Any] = {"role": "user", "agent": "build", "mode": "build",
                                        "modelID": "", "providerID": "", "cost": 0,
                                        "tokens": {"input": 0, "output": 0, "reasoning": 0,
                                                   "cache": {"read": 0, "write": 0}}}
            if src_sid:
                row = c.execute(
                    "select data from message where session_id=? order by time_updated desc limit 1",
                    (src_sid,)).fetchone()
                if row:
                    try:
                        base = json.loads(row[0])
                        for k in ("agent", "mode", "modelID", "providerID", "variant"):
                            if k in base:
                                template[k] = base[k]
                    except json.JSONDecodeError:
                        pass
            proj = c.execute("select project_id from session where id=?", (src_sid,)).fetchone() \
                if src_sid else None
            c.execute(
                "insert into session (id, project_id, slug, directory, title, version, cost,"
                " tokens_input, tokens_output, tokens_reasoning, tokens_cache_read,"
                " tokens_cache_write, time_created, time_updated)"
                " values (?,?,?,?,?,?,0,0,0,0,0,0,?,?)",
                (sid, proj[0] if proj else "", f"codec-{sid[-6:]}", session.project,
                 f"codec compacted {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
                 "1", now_ms, now_ms))
            parent: str | None = None
            for m in session.messages:
                if m.role == "tool":
                    continue
                mid = _new_id("msg")
                mdata = dict(template)
                mdata["parentID"] = parent
                mdata["role"] = m.role
                mdata["path"] = {"cwd": session.project, "root": "/"}
                mdata["time"] = {"created": now_ms}
                c.execute("insert into message (id, session_id, time_created, time_updated, data)"
                          " values (?,?,?,?,?)", (mid, sid, now_ms, now_ms, json.dumps(mdata)))
                pid = _new_id("prt")
                c.execute("insert into part (id, message_id, session_id, time_created, time_updated, data)"
                          " values (?,?,?,?,?,?)",
                          (pid, mid, sid, now_ms, now_ms,
                           json.dumps({"type": "text", "text": m.content})))
                parent = mid
            c.commit()
        session.meta["new_session_id"] = sid
        return Path(str(DB_FILE))

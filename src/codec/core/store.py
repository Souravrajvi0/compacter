"""On-disk master transcript: <project>/.codec/session.jsonl.

Line 1 is the session header, every following line is one message.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .models import Message, Session

STORE_DIR = ".codec"
STORE_FILE = "session.jsonl"


def store_path(project: Path) -> Path:
    return project / STORE_DIR / STORE_FILE


def save(session: Session, project: Path) -> Path:
    path = store_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(session.header())] + [json.dumps(m.to_dict()) for m in session.messages]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written transcript
    return path


def load(project: Path) -> Session:
    path = store_path(project)
    if not path.exists():
        raise FileNotFoundError(f"no codec session in {project} (run `codec start` first)")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    h = rows[0]
    return Session(
        agent=h["agent"], project=h["project"], id=h["id"], created_at=h["created_at"],
        system=h.get("system", ""), tool_schemas=h.get("tool_schemas", []),
        meta=h.get("meta", {}), messages=[Message.from_dict(r) for r in rows[1:]],
    )

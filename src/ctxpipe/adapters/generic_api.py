"""Generic adapter: plain JSON list of {role, content} chat messages (OpenAI/Anthropic style).

Reads/writes <project>/messages.json. Useful for tests and for agents without a session file.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..core.models import Message, Session
from .base import Adapter, register

IN_FILE = "messages.json"
OUT_FILE = "messages.compacted.json"


@register
class GenericAPIAdapter(Adapter):
    name = "generic-api"

    def read(self, project: Path) -> Session:
        rows = json.loads((project / IN_FILE).read_text(encoding="utf-8", errors="replace"))
        return Session(
            agent=self.name, project=str(project.resolve()),
            messages=[Message(role=r["role"], content=r["content"],
                              meta={"pair_ids": r["pair_ids"]} if r.get("pair_ids") else {})
                      for r in rows],
        )

    def write(self, session: Session, project: Path) -> Path:
        out = project / OUT_FILE
        out.write_text(
            json.dumps([{"role": m.role, "content": m.content} for m in session.messages], indent=2),
            encoding="utf-8",
        )
        return out

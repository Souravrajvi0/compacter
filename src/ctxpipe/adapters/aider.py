"""Aider adapter. Reads/writes .aider.chat.history.md.

Format: blocks introduced by `#### user` / `#### assistant` headers, blank line,
then free text up to the next header. Writes go to a new *.compacted.md file.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core.models import Message, Session
from .base import Adapter, register

IN_FILE = ".aider.chat.history.md"
OUT_FILE = ".aider.chat.history.compacted.md"
HEADER = re.compile(r"^#### (user|assistant|system)\s*$")


@register
class AiderAdapter(Adapter):
    name = "aider"

    def read(self, project: Path) -> Session:
        src = project / IN_FILE
        if not src.exists():
            raise FileNotFoundError(f"no Aider history at {src}")
        session = Session(agent=self.name, project=str(project.resolve()), meta={"source_file": str(src)})
        role: str | None = None
        buf: list[str] = []
        for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
            m = HEADER.match(line)
            if m:
                if role is not None and "\n".join(buf).strip():
                    session.messages.append(Message(role=role, content="\n".join(buf).strip()))
                role, buf = m.group(1), []
            elif role is not None:
                buf.append(line)
        if role is not None and "\n".join(buf).strip():
            session.messages.append(Message(role=role, content="\n".join(buf).strip()))
        return session

    def write(self, session: Session, project: Path) -> Path:
        out = project / OUT_FILE
        blocks = []
        for m in session.messages:
            if m.role not in ("user", "assistant", "system"):
                continue
            blocks.append(f"#### {m.role}\n\n{m.content}\n")
        out.write_text("\n".join(blocks), encoding="utf-8")
        return out

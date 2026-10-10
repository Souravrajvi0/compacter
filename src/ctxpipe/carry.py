"""Carry chosen messages from one chat into a new one.

Pick messages (checklist / ranges / ids), summarize them (or keep them verbatim), save the result as a
context pack, and optionally write a brand-new chat that starts with it. The source chat is untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .adapters import get_adapter
from .adapters.base import resume_command
from .core import compaction, packs
from .core.models import Session


@dataclass
class CarryResult:
    name: str
    path: Path
    count: int
    verbatim: bool
    new_chat: Path | None = None
    resume: str | None = None
    new_session_id: str | None = None


def default_name() -> str:
    return datetime.now().strftime("carry-%Y%m%d-%H%M%S")


def carry(session: Session, ids: set[str], project: Path, *, name: str | None = None, verbatim: bool = False,
          llm_name: str = "auto", to_agent: str | None = None, new_chat: bool = False,
          scopes=None) -> CarryResult:
    chosen = [m for m in session.messages if m.id in ids]
    if not chosen:
        raise ValueError("no messages selected")
    name = name or default_name()
    summary = None
    if not verbatim:
        from .llm import get_llm

        summary = compaction.summarize(compaction.CompactionPlan(to_summarize=chosen),
                                       get_llm(llm_name, agent=session.agent), scopes=scopes)
    path = packs.carry_pack(name, session, chosen, summary)
    res = CarryResult(name=name, path=path, count=len(chosen), verbatim=verbatim)
    if new_chat:
        target = to_agent or session.agent
        seeded = packs.seed_session(packs.load_pack(name), target, project)
        res.new_chat = get_adapter(target).write(seeded, project)
        res.new_session_id = seeded.meta.get("new_session_id")
        res.resume = resume_command(target, res.new_session_id, res.new_chat)
    return res

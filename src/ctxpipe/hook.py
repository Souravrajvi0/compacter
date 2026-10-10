"""`/compacter` prompt-hook handler for agents that expose a UserPromptSubmit hook.

Claude Code (verified) and Codex (hooks; unverified here) send the typed prompt as JSON on stdin.
We answer `/compacter ...` ourselves and block the prompt, so no model turn is spent and the
text below is shown to the user in the same terminal.

    /compacter                list older messages, each with a [✓] on the right
    /compacter all            compact every listed message
    /compacter skip 4,7-9     compact everything except these (they stay verbatim)
    /compacter only 0-12      compact only these
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .adapters import get_adapter
from .adapters.base import resume_command
from .config import load_config
from .core import compaction, store
from .core.tokens import estimate_tokens
from .tui import parse_ranges

TRIGGER = re.compile(r"^\s*/(?:prompts:)?compacter\b(.*)$", re.S)
WIDTH = 92
USAGE = ("Usage:  /compacter            list messages ([✓] = will be compacted)\n"
         "        /compacter all        compact all listed messages\n"
         "        /compacter skip 4,7-9 compact all except these indexes (kept verbatim)\n"
         "        /compacter only 0-12  compact only these indexes")


def _rows(s, keep_recent: int, scopes):
    base = compaction.plan(s, keep_recent=keep_recent, scopes=scopes)
    ok = {m.id for m in base.to_summarize + base.dropped}
    return [(i, m) for i, m in enumerate(s.messages) if m.id in ok]


def _listing(rows, chosen: set[str]) -> str:
    out = []
    for i, m in rows:
        text = " ".join(m.content.split())
        room = WIDTH - 16
        text = text[: room - 1] + "…" if len(text) > room else text
        out.append(f"{i:>3} {m.role:<9} {text:<{room}} [{'✓' if m.id in chosen else ' '}]")
    return "\n".join(out)


class Inject(str):
    """Marks a reply that should be added to the model's context instead of blocking the prompt."""


PACK_USAGE = ("Context packs (reuse a compaction in a new chat):\n"
              "  /compacter pack save NAME   save the latest compaction + pinned messages\n"
              "  /compacter pack list        show saved packs\n"
              "  /compacter pack load NAME   add a pack to THIS chat's context (use at the start of a new chat)\n"
              "  /compacter pack new NAME    write a brand-new chat that starts with the pack\n"
              "  /compacter pack delete NAME")


def _pack(args: list[str], project: Path, agent: str) -> str:
    from .core import packs

    action, name = (args + ["", ""])[:2]
    try:
        if action == "list":
            rows = packs.list_packs()
            return "\n".join(f"{r['name']:<24} {r['created_at']}  {r['agent']}" for r in rows) or "No packs yet."
        if action == "save":
            path = packs.save_pack(name, adapter_session(project, agent))
            return f"Saved pack {name!r} -> {path}"
        if action == "delete":
            return "Deleted." if packs.delete_pack(name) else f"No pack named {name!r}."
        if action == "load":
            return Inject(packs.render_markdown(packs.load_pack(name)))
        if action == "new":
            seeded = packs.seed_session(packs.load_pack(name), agent, project)
            out = get_adapter(agent).write(seeded, project)
            return (f"New chat seeded with pack {name!r}. Continue on it:\n"
                    f"  {resume_command(agent, seeded.meta.get('new_session_id'), out)}")
    except (ValueError, FileNotFoundError) as e:
        return str(e)
    return PACK_USAGE


CARRY_USAGE = ("Carry chosen messages into a new chat:\n"
               "  /compacter carry                      list every message ([ ] = not chosen)\n"
               "  /compacter carry 3-9,14               summarize those messages into a pack\n"
               "  /compacter carry 3-9 verbatim         carry them word for word instead\n"
               "  /compacter carry 3-9 as auth-fix new  name the pack and also write a ready-to-resume new chat\n"
               "Then, in the new chat:  /compacter pack load NAME")


def _carry(args: list[str], project: Path, agent: str, llm_name: str) -> str:
    from .carry import carry
    from .core.models import Session  # noqa: F401

    adapter = get_adapter(agent)
    ranges, verbatim, new_chat, name = None, False, False, None
    it = iter(args)
    for tok in it:
        low = tok.lower()
        if low == "verbatim":
            verbatim = True
        elif low == "new":
            new_chat = True
        elif low == "as":
            name = next(it, None)
        elif re.fullmatch(r"-?\d+(?:-\d+)?(?:,-?\d+(?:-\d+)?)*", tok):
            ranges = tok
        else:
            return f"Unknown option {tok!r}.\n{CARRY_USAGE}"
    try:
        if ranges is None:
            s = adapter.read(project)
            s.meta["carry_listed"] = True
            store.save(s, project)
            rows = list(enumerate(s.messages))
            if not rows:
                return "This chat is empty."
            return f"{len(rows)} messages\n{_listing(rows, set())}\n\n{CARRY_USAGE}"
        try:
            s = store.load(project)
            if not s.meta.pop("carry_listed", False) or s.agent != agent:
                s = adapter.read(project)
        except FileNotFoundError:
            s = adapter.read(project)
        chosen = parse_ranges(ranges, len(s.messages))
        ids = {m.id for i, m in enumerate(s.messages) if i in chosen}
        res = carry(s, ids, project, name=name, verbatim=verbatim, llm_name=llm_name, to_agent=agent, new_chat=new_chat,
                    scopes=load_config(project).scopes)
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        return str(e)
    how = "word for word" if res.verbatim else "summarized"
    out = f"Carried {res.count} messages ({how}) into pack {res.name!r}. The original chat is untouched.\n"
    if res.resume:
        return out + f"New chat is ready:\n  {res.resume}"
    return out + f"In a new chat, run:  /compacter pack load {res.name}"


def adapter_session(project: Path, agent: str):
    """The session to save a pack from: the stored master transcript if it has a compaction, else a fresh import."""
    from .core import packs

    try:
        s = store.load(project)
        if s.agent == agent and packs._latest_summary(s) is not None:
            return s
    except FileNotFoundError:
        pass
    return get_adapter(agent).read(project)


def respond(prompt: str, project: Path, agent: str, keep_recent: int = 6, llm_name: str = "auto") -> str | None:
    """Text to show the user, or None if the prompt is not ours."""
    m = TRIGGER.match(prompt)
    if not m:
        return None
    words = m.group(1).split()
    verb = words[0].lower() if words else "list"
    if verb in ("help", "-h", "--help"):
        return USAGE + "\n\n" + CARRY_USAGE + "\n\n" + PACK_USAGE
    if verb == "pack":
        return _pack(words[1:], project, agent)
    if verb == "carry":
        return _carry(words[1:], project, agent, llm_name)
    cfg = load_config(project)
    adapter = get_adapter(agent)

    if verb == "list":
        s = adapter.read(project)
        s.meta["picker_listed"] = True
        store.save(s, project)
        rows = _rows(s, keep_recent, cfg.scopes)
        if not rows:
            return "Nothing old enough to compact yet."
        every = {x.id for _, x in rows}
        tokens = sum(estimate_tokens(x.content) for _, x in rows)
        return (f"{len(rows)} messages can be compacted (~{tokens} tokens; the last {keep_recent} stay as-is)\n"
                f"{_listing(rows, every)}\n\n{USAGE}")

    if verb not in ("all", "skip", "only"):
        return f"Unknown option {verb!r}.\n{USAGE}"
    try:
        s = store.load(project)
        if not s.meta.pop("picker_listed", False) or s.agent != agent:
            s = adapter.read(project)
    except FileNotFoundError:
        s = adapter.read(project)
    rows = _rows(s, keep_recent, cfg.scopes)
    if not rows:
        return "Nothing old enough to compact yet."
    ids = {x.id for _, x in rows}
    try:
        picked = parse_ranges(" ".join(words[1:]).replace(" ", ","), len(s.messages)) if verb != "all" else set()
    except ValueError as e:
        return f"{e}\n{USAGE}"
    if verb == "only":
        chosen = {x.id for i, x in rows if i in picked}
    elif verb == "skip":
        chosen = {x.id for i, x in rows if i not in picked}
    else:
        chosen = ids
    if not chosen:
        return "Nothing selected; nothing changed."

    from .llm import get_llm

    p = compaction.plan(s, keep_recent=keep_recent, scopes=cfg.scopes, only_ids=chosen)
    prev = None
    for x in s.messages:
        if "compaction_summary" in x.tags and x.meta.get("summary_json"):
            try:
                prev = json.loads(x.meta["summary_json"])
            except ValueError:
                pass
    summary = compaction.summarize(p, get_llm(llm_name, agent=agent), previous_summary=prev, scopes=cfg.scopes)
    view = compaction.rebuild(s, p, summary, scopes=cfg.scopes)
    out = adapter.write(view, project)
    view.meta.setdefault("compactions", []).append({
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summarized": len(p.to_summarize), "dropped": len(p.dropped), "excluded": len(p.excluded),
        "tokens_before": p.tokens_before(), "tokens_after": compaction.tokens_after(view),
    })
    store.save(view, project)
    sid = view.meta.get("new_session_id") or out.stem
    resume = resume_command(agent, sid, out)
    return (f"Compacted {len(chosen)} messages: ~{p.tokens_before()} -> ~{compaction.tokens_after(view)} tokens.\n"
            f"The original session is untouched. Continue on the compacted one:\n  {resume}\n"
            f"(or /resume and pick the newest session)")


def run(agent: str, keep_recent: int = 6, llm_name: str = "auto") -> int:
    """Hook entry point: JSON on stdin, JSON decision on stdout."""
    if os.environ.get("CTXPIPE_NESTED"):  # our own summarizer subprocess: never recurse
        return 0
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    prompt = payload.get("prompt") or ""
    if not TRIGGER.match(prompt):
        return 0
    project = Path(payload.get("cwd") or ".")
    try:
        text = respond(prompt, project, agent, keep_recent, llm_name)
    except Exception as e:  # never let a hook crash the agent
        text = f"ctxpipe error: {e}"
    if isinstance(text, Inject):
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": str(text)}}))
    elif text is not None:
        sys.stdout.write(json.dumps({"decision": "block", "reason": text}))
    return 0

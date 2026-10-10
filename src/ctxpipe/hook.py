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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .adapters import get_adapter
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


def respond(prompt: str, project: Path, agent: str, keep_recent: int = 6, llm_name: str = "anthropic") -> str | None:
    """Text to show the user, or None if the prompt is not ours."""
    m = TRIGGER.match(prompt)
    if not m:
        return None
    words = m.group(1).split()
    verb = words[0].lower() if words else "list"
    if verb in ("help", "-h", "--help"):
        return USAGE
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
    summary = compaction.summarize(p, get_llm(llm_name), previous_summary=prev, scopes=cfg.scopes)
    view = compaction.rebuild(s, p, summary, scopes=cfg.scopes)
    out = adapter.write(view, project)
    view.meta.setdefault("compactions", []).append({
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summarized": len(p.to_summarize), "dropped": len(p.dropped), "excluded": len(p.excluded),
        "tokens_before": p.tokens_before(), "tokens_after": compaction.tokens_after(view),
    })
    store.save(view, project)
    sid = view.meta.get("new_session_id") or out.stem
    resume = {"claude-code": f"claude --resume {sid}", "codex": "codex resume --last"}.get(agent, str(out))
    return (f"Compacted {len(chosen)} messages: ~{p.tokens_before()} -> ~{compaction.tokens_after(view)} tokens.\n"
            f"The original session is untouched. Continue on the compacted one:\n  {resume}\n"
            f"(or /resume and pick the newest session)")


def run(agent: str, keep_recent: int = 6, llm_name: str = "anthropic") -> int:
    """Hook entry point: JSON on stdin, JSON decision on stdout."""
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
    if text is not None:
        sys.stdout.write(json.dumps({"decision": "block", "reason": text}))
    return 0

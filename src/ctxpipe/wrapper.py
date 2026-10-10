"""Live wrapper mode: run the agent with stdio passthrough, intercept /compact.

Experimental: this is a line-oriented stdio shim, not a PTY. It pumps the
agent's output through and forwards user input, except /compact, which it
handles itself: compact via the master transcript, write a new session through
the adapter, then stop the child so the user can relaunch with --resume.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from .adapters import get_adapter
from .core import compaction, store

COMPACT_LINE = "/compact"


def is_compact_command(line: str) -> bool:
    return line.strip() == COMPACT_LINE


def _pump(stream, target) -> None:
    try:
        for data in iter(stream.readline, ""):
            if not data:
                break
            target.write(data)
            target.flush()
    except (ValueError, OSError):
        pass


def handle_compact(project: Path, agent: str, llm_name: str, keep_recent: int) -> None:
    from .llm import get_llm

    try:
        s = store.load(project)
    except FileNotFoundError:
        s = get_adapter(agent).read(project)
        store.save(s, project)
    p = compaction.plan(s, keep_recent=keep_recent)
    summary = compaction.summarize(p, get_llm(llm_name, agent=agent))
    view = compaction.rebuild(s, p, summary)
    out = get_adapter(agent).write(view, project)
    view.meta.setdefault("compactions", []).append({
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summarized": len(p.to_summarize),
        "dropped": len(p.dropped),
        "tokens_before": p.tokens_before(),
        "tokens_after": compaction.tokens_after(view),
    })
    store.save(view, project)
    print(f"\n[ctxpipe] compacted ~{p.tokens_before()} -> ~{compaction.tokens_after(view)} tokens")
    print(f"[ctxpipe] wrote {out}")
    print("[ctxpipe] relaunch the agent with --resume to continue on the compacted session")


def run(project: Path, agent: str, command: list[str], llm_name: str = "auto",
        keep_recent: int = 6, auto_compact: str = "off") -> int:
    child = subprocess.Popen(
        command, cwd=project,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    threading.Thread(target=_pump, args=(child.stdout, sys.stdout), daemon=True).start()
    try:
        for line in sys.stdin:
            if is_compact_command(line):
                print("\n[ctxpipe] /compact intercepted", flush=True)
                try:
                    handle_compact(project, agent, llm_name, keep_recent)
                except (FileNotFoundError, LookupError, ValueError, RuntimeError) as e:
                    print(f"[ctxpipe] error: {e}", flush=True)
                try:
                    child.stdin.close()
                except (OSError, ValueError):
                    pass
                try:
                    return child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    try:
                        return child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        return child.wait()
            if child.poll() is not None:
                break
            child.stdin.write(line)
            child.stdin.flush()
            if auto_compact != "off":
                rc = _maybe_auto_compact(project, agent, llm_name, keep_recent, auto_compact, line, child)
                if rc is not None:
                    return rc
    except KeyboardInterrupt:
        child.terminate()
    finally:
        try:
            child.stdin.close()
        except (OSError, ValueError):
            pass
    try:
        return child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        return child.wait()


def _terminal_ui():
    """Prompt on stderr, read from the real terminal (never from the child's pipe)."""
    from .core.autocontrol import PromptUI

    def _read(_prompt: str) -> str:
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return line.rstrip("\n")

    return PromptUI(input_fn=_read, output_fn=lambda text: (sys.stderr.write(text), sys.stderr.flush()))


def _maybe_auto_compact(project: Path, agent: str, llm_name: str, keep_recent: int,
                        mode: str, line: str, child, ui=None, interactive: bool | None = None) -> "int | None":
    """Hook called at each user message (a natural safe boundary)."""
    from .config import load_config
    from .core.autocontrol import AutoCompactController, capture_checkpoint

    try:
        cfg = load_config(project)
    except FileNotFoundError:
        return None
    acfg = cfg.auto_compaction
    if mode == "off":
        return None
    if mode == "ask":
        acfg.enabled = True
        acfg.ask_user = "always"
    elif mode == "apply":
        acfg.enabled = True
        acfg.ask_user = "never"
    if not acfg.enabled:
        return None

    try:
        s = store.load(project)
    except FileNotFoundError:
        try:
            s = get_adapter(agent).read(project)
        except FileNotFoundError:
            return None
        store.save(s, project)

    # record the user turn so the controller sees it
    from .core.models import Message as _M

    s.messages.append(_M(role="user", content=line.strip()))
    store.save(s, project)

    controller = AutoCompactController(s, acfg)
    if interactive is None:
        interactive = sys.stdin.isatty()
    if interactive and ui is None:
        ui = _terminal_ui()
    from .llm import get_llm

    llm = None

    def apply_fn(session):
        nonlocal llm
        if llm is None:
            llm = get_llm(llm_name, agent=agent)
        p = compaction.plan(session, keep_recent=keep_recent, scopes=cfg.scopes)
        summary = compaction.summarize(p, llm, previous_summary=_previous_summary(session), scopes=cfg.scopes)
        return compaction.rebuild(session, p, summary, scopes=cfg.scopes)

    result = controller.evaluate(keep_recent=keep_recent, scopes=cfg.scopes,
                                 checkpoint=capture_checkpoint(s), interactive=interactive,
                                 ui=ui, apply_fn=apply_fn)
    store.save(s, project)
    action = result.get("action")
    if action == "applied":
        out = get_adapter(agent).write(s, project)
        print(f"\n[ctxpipe] auto-compaction applied -> {out}", flush=True)
        try:
            child.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            return child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.terminate()
            return child.wait()
    if action in ("pending_boundary", "cooldown", "cooldown_turns", "cooldown_tokens",
                  "waiting_required", "postponed_noninteractive", "disabled_for_session",
                  "circuit_breaker", "rejected", "skipped", "below_threshold"):
        print(f"[ctxpipe] auto-compact: {action}", flush=True)
    return None


def _previous_summary(session):
    import json

    for m in session.messages:
        if "compaction_summary" in m.tags and m.meta.get("summary_json"):
            try:
                return json.loads(m.meta["summary_json"])
            except ValueError:
                continue
    return None

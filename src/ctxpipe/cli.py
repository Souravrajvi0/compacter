"""ctxpipe CLI. Thin layer: parse args, call core + adapters, print."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .adapters import get_adapter
from .config import load_config
from .core import compaction, store
from .core.models import Policy
from .core.tokens import estimate_tokens, session_tokens

POLICIES = [p.value for p in Policy]


def _project(args: argparse.Namespace) -> Path:
    return Path(args.project).resolve()


def cmd_start(args: argparse.Namespace) -> int:
    project = _project(args)
    session = get_adapter(args.agent).read(project)
    path = store.save(session, project)
    print(f"imported {len(session.messages)} messages from {args.agent} -> {path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    s = store.load(_project(args))
    print(f"{s.agent} | {len(s.messages)} messages | ~{session_tokens(s.visible())} visible tokens")
    for i, m in enumerate(s.messages):
        tags = ",".join(sorted(m.tags)) or "-"
        print(f"{i:>3} {m.id} {m.role:<9} {m.policy.value:<13} [{tags}] {m.content[:60]!r}")
    return 0


def cmd_tag(args: argparse.Namespace) -> int:
    project = _project(args)
    s = store.load(project)
    m = s.resolve(args.target)
    m.tags.add(args.as_tag)
    store.save(s, project)
    print(f"tagged {m.id} as {args.as_tag}")
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    project = _project(args)
    s = store.load(project)
    m = s.resolve(args.target)
    m.policy = Policy(args.policy)
    store.save(s, project)
    print(f"marked {m.id} as {m.policy.value}")
    return 0


def cmd_drop_last(args: argparse.Namespace) -> int:
    project = _project(args)
    s = store.load(project)
    for m in s.messages[-args.n:]:
        m.policy = Policy.DROP_ALWAYS
    store.save(s, project)
    print(f"marked last {args.n} messages as drop_always")
    return 0


def _choose_messages(args, s, keep_recent, scopes):
    """--select / --only: ids the user chose to compact (None = no restriction)."""
    if not (args.select or args.only or args.only_ids):
        return None
    base = compaction.plan(s, keep_recent=keep_recent, scopes=scopes)
    ok = {m.id for m in base.to_summarize + base.dropped}
    rows = [(i, m) for i, m in enumerate(s.messages) if m.id in ok]
    if not rows:
        print("no compactable messages outside the keep-recent window")
        return set()
    if args.only_ids:
        raw = sys.stdin.read() if args.only_ids == "-" else args.only_ids
        wanted = {t for t in raw.replace(",", " ").split() if t}
        return {m.id for _, m in rows if m.id in wanted or m.meta.get("source_id") in wanted}
    if args.only:
        from .tui import parse_ranges

        try:
            chosen = parse_ranges(args.only, len(s.messages))
        except ValueError as e:
            raise SystemExit(f"--only: {e}")
        return {m.id for i, m in rows if i in chosen}
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit("--select needs an interactive terminal; use --only 0,3-5 instead")
    from .tui import pick_messages

    picked = pick_messages([m for _, m in rows], [i for i, _ in rows], set(ok))
    return "cancelled" if picked is None else picked


def cmd_compact(args: argparse.Namespace) -> int:
    from .core.prompts import PROFILES
    from .llm import get_llm

    project = _project(args)
    s = store.load(project)
    cfg = load_config(project)

    if args.checkpoint:
        from .core.autocontrol import capture_checkpoint

        print(json.dumps(capture_checkpoint(s), indent=2))
        return 0

    if args.verify:
        from .core.autocontrol import pending_essences, _open_pair_ids
        from .core.summary import parse_structured_summary

        problems = []
        opens = _open_pair_ids(s)
        if opens:
            problems.append(f"open tool pairs: {sorted(opens)}")
        for m in s.messages:
            if "compaction_summary" in m.tags and m.meta.get("summary_json"):
                if parse_structured_summary(m.meta["summary_json"]) is None:
                    problems.append(f"summary message {m.id} has invalid summary_json")
        if any(m.policy is Policy.DROP_ALWAYS for m in s.messages):
            problems.append("drop_always message present")
        eff = pending_essences(s)
        print(f"essences pending: {len(eff)}")
        if problems:
            for p in problems:
                print(f"FAIL: {p}")
            return 2
        print("transcript OK: no open pairs, summary_json valid, no drop_always leaks")
        return 0

    if args.review:
        keep_recent = args.keep_recent or 6
        p = compaction.plan(s, keep_recent=keep_recent, scopes=cfg.scopes)
        from .core.autocontrol import _summary_high_of, review_diff, review_items, review_summary

        if args.review == "summary":
            print(review_summary(p), end="")
            print(f"EXCLUDED:  {len(p.excluded)} messages (drop_always)")
        elif args.review == "items":
            print("\n".join(review_items(s, p)))
        else:  # diff: previous summary vs. a freshly built candidate (needs an LLM)
            from .core.summary import parse_structured_summary
            from .llm import get_llm

            summary = compaction.summarize(p, get_llm(args.llm), args.target_tokens,
                                           previous_summary=_summary_high_of(s), scopes=cfg.scopes)
            print(review_diff(_summary_high_of(s), parse_structured_summary(summary)))
        return 0

    if args.auto or args.apply_if_valid:
        from .core.autocontrol import AutoCompactController, capture_checkpoint
        from .llm import get_llm

        if args.apply_if_valid:
            cfg.auto_compaction.ask_user = "never"
            cfg.auto_compaction.noninteractive_action = "apply-if-valid"
        s_meta = s
        ctrl = AutoCompactController(s, cfg.auto_compaction)
        llm = get_llm(args.llm)
        ckpt = capture_checkpoint(s)

        def apply_fn(session):
            p = compaction.plan(session, keep_recent=(args.keep_recent or cfg.auto_compaction.keep_recent), scopes=cfg.scopes)
            prev = None
            for m in session.messages:
                if "compaction_summary" in m.tags and m.meta.get("summary_json"):
                    try:
                        prev = json.loads(m.meta["summary_json"])
                    except ValueError:
                        pass
            summary = compaction.summarize(p, llm, args.target_tokens, previous_summary=prev,
                                           profile=args.profile, scopes=cfg.scopes)
            return compaction.rebuild(session, p, summary, token_budget=args.target_tokens, scopes=cfg.scopes)

        interactive = sys.stdin.isatty()
        ui = None
        if interactive:
            from .core.autocontrol import PromptUI

            ui = PromptUI()
        result = ctrl.evaluate(keep_recent=args.keep_recent or cfg.auto_compaction.keep_recent, scopes=cfg.scopes,
                               checkpoint=ckpt, interactive=interactive, apply_fn=apply_fn, ui=ui)
        store.save(s, project)
        print(f"auto-compact: {result.get('action')}")
        if result.get("assessment"):
            a = result["assessment"]
            print(f"  pressure={a['effective_pressure']:.0%} reduction={a['projected_reduction']:.0%} risk={a['semantic_risk']} rec={a['recommendation']}")
        if result.get("verification"):
            v = result["verification"]
            print(f"  verification passed={v['passed']} repair={v['repair_attempted']}")
            for e in v.get("errors", [])[:3]:
                print(f"    error: {e}")
        if result.get("action") == "applied":
            out = get_adapter(s.agent).write(s, project)
            print(f"wrote compacted session -> {out}")
        return 0

    keep_recent = args.keep_recent
    if keep_recent is None:
        keep_recent = {"balanced": 6, "conservative": 10, "aggressive": 3, "code-heavy": 6}.get(args.profile, 6)
    visible_tokens = session_tokens(s.visible()) + estimate_tokens(s.system)
    if args.threshold and visible_tokens <= args.threshold:
        print(f"~{visible_tokens} tokens, under --threshold {args.threshold}; nothing to do")
        return 0
    only_ids = _choose_messages(args, s, keep_recent, cfg.scopes)
    if only_ids == "cancelled":
        print("cancelled; nothing changed")
        return 1
    p = compaction.plan(s, keep_recent=keep_recent, scopes=cfg.scopes, only_ids=only_ids)
    print(f"pinned={len(p.pinned)} summarize={len(p.to_summarize)} "
          f"dropped={len(p.dropped)} excluded={len(p.excluded)} recent={len(p.recent)}")
    previous = None
    for m in s.messages:
        if "compaction_summary" in m.tags and m.meta.get("summary_json"):
            try:
                previous = json.loads(m.meta["summary_json"])
            except ValueError:
                previous = None
    summary = compaction.summarize(
        p, get_llm(args.llm), args.target_tokens, previous_summary=previous,
        profile=args.profile, scopes=cfg.scopes,
    )
    view = compaction.rebuild(s, p, summary, token_budget=args.target_tokens, scopes=cfg.scopes)
    print(f"~{p.tokens_before()} -> ~{compaction.tokens_after(view)} tokens")
    if not args.apply:
        print("\n--- summary preview ---\n" + (summary or "(nothing to summarize)"))
        return 0
    out = get_adapter(s.agent).write(view, project)
    view.meta.setdefault("compactions", []).append({
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summarized": len(p.to_summarize),
        "dropped": len(p.dropped),
        "excluded": len(p.excluded),
        "tokens_before": p.tokens_before(),
        "tokens_after": compaction.tokens_after(view),
    })
    store.save(view, project)
    if new_id := view.meta.get("new_session_id"):
        print(f"CTXPIPE_NEW_SESSION={new_id}")
    print(f"wrote compacted session -> {out}; master transcript updated at {store.store_path(project)}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from . import evals
    from .llm import get_llm

    project = _project(args)
    s = store.load(project)
    p = compaction.plan(s, keep_recent=args.keep_recent)
    summary = compaction.summarize(p, get_llm(args.llm), args.target_tokens)
    view = compaction.rebuild(s, p, summary)
    result = evals.judge(view, args.task, get_llm(args.llm), original=s)
    print("PASS" if result.passed else "FAIL", "-", result.explanation)
    if result.metrics:
        print(json.dumps(result.metrics, indent=2))
    return 0 if result.passed else 2


def _auto_controller(project: Path, cfg):
    from .core.autocontrol import AutoCompactController

    s = store.load(project)
    return s, AutoCompactController(s, cfg.auto_compaction)


def cmd_ac_status(args: argparse.Namespace) -> int:
    from .config import load_config

    project = _project(args)
    cfg = load_config(project)
    s = store.load(project)
    meta = s.meta.get("auto_compaction", {})
    last_idx = int(meta.get("last_compaction_msg_count", 0))
    turns_since = len(s.messages) - last_idx
    new_tokens = session_tokens(s.messages[last_idx:]) if last_idx else session_tokens(s.messages)
    from .core.autocontrol import AutoCompactController, _system_overhead

    ctrl = AutoCompactController(s, cfg.auto_compaction)
    pressure = ctrl.pressure_percent()
    print(f"Automatic compaction: {'enabled' if cfg.auto_compaction.enabled else 'disabled'}")
    print(f"State: {meta.get('state', 'IDLE')}")
    print(f"Session-only disabled: {meta.get('disabled_session', False)}")
    print(f"Context pressure: {int(pressure * 100)}%")
    print(f"Evaluation threshold: {cfg.auto_compaction.evaluate_at_percent}%")
    print(f"Recommendation threshold: {cfg.auto_compaction.recommend_at_percent}%")
    print(f"Force threshold: {cfg.auto_compaction.force_at_percent}%")
    cd = ctrl.cooldown_turns_left()
    print(f"Cooldown: {'active (' + str(cd) + ' turns)' if cd > 0 else 'inactive'}")
    print(f"Armed: {meta.get('armed', True)}")
    print(f"Rollback snapshot: {'available' if meta.get('rollback') else 'none'}")
    print(f"Last compaction: {turns_since} turns ago")
    print(f"New tokens since compaction: {new_tokens:,}")
    return 0


def cmd_ac_enable(args: argparse.Namespace) -> int:
    return _set_auto(_project(args), True)


def cmd_ac_disable(args: argparse.Namespace) -> int:
    return _set_auto(_project(args), False)


def _set_auto(project: Path, enabled: bool) -> int:
    import yaml

    path = project / "ctxpipe.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw = raw or {}
    raw.setdefault("auto_compaction", {})["enabled"] = enabled
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    print(f"auto_compaction.enabled = {enabled} in {path}")
    return 0


def cmd_ac_assess(args: argparse.Namespace) -> int:
    from .config import load_config
    from .core.autocontrol import assess, capture_checkpoint

    project = _project(args)
    cfg = load_config(project)
    s = store.load(project)
    ckpt = capture_checkpoint(s)
    a = assess(s, cfg.auto_compaction, scopes=cfg.scopes, checkpoint=ckpt)
    print(json.dumps({
        "current_tokens": a.current_tokens,
        "usable_context_tokens": a.usable_context_tokens,
        "effective_pressure": a.effective_pressure,
        "projected_output_tokens": a.projected_output_tokens,
        "projected_reduction": a.projected_reduction,
        "semantic_risk": a.semantic_risk,
        "recommendation": a.recommendation,
        "reasons": a.reasons,
        "blockers": a.blockers,
        "removable_tool_ratio": a.removable_tool_ratio,
        "pinned_content_ratio": a.pinned_content_ratio,
        "completed_work_ratio": a.completed_work_ratio,
        "exact_fact_density": a.exact_fact_density,
        "checkpoint_phase": ckpt["phase"],
    }, indent=2))
    return 0


def cmd_ac_rollback(args: argparse.Namespace) -> int:
    from .config import load_config

    project = _project(args)
    cfg = load_config(project)
    s, ctrl = _auto_controller(project, cfg)
    if not ctrl.rollback():
        print("no rollback snapshot available")
        return 1
    store.save(s, project)
    print(f"master transcript restored ({len(s.messages)} messages)."
          " The agent's own session file is unchanged; relaunch it from the master if needed.")
    return 0


def cmd_ac_reset(args: argparse.Namespace) -> int:
    from .config import load_config

    project = _project(args)
    cfg = load_config(project)
    s, ctrl = _auto_controller(project, cfg)
    ctrl.reset_cooldown()
    store.save(s, project)
    print("cooldown metadata reset")
    return 0


def cmd_auto_tag(args: argparse.Namespace) -> int:
    from .core import autotag

    project = _project(args)
    s = store.load(project)
    cfg = load_config(project)
    if not cfg.auto_tagging.enabled:
        print("auto-tagging disabled in ctxpipe.yaml")
        return 0
    min_conf = args.min_confidence if args.min_confidence is not None else cfg.auto_tagging.min_confidence
    suggestions = autotag.suggest(s, min_confidence=min_conf)
    if not suggestions:
        print("no suggestions")
        return 0
    for sug in suggestions:
        print(f"{sug.message.id}  +{sug.tag}  ({sug.confidence:.2f})  -- {sug.reason}")
    if not args.apply:
        print("\n(hint: run with --apply to write these)")
        return 0
    keep = set(cfg.auto_tagging.set_keep_verbatim_for_tags)
    if args.also_set_keep_verbatim:
        keep |= {"goal", "constraint"}
    n = autotag.apply(s, suggestions, keep_verbatim_for=keep)
    store.save(s, project)
    print(f"applied {n} tags" + (f"; keep_verbatim set for {sorted(keep)}" if keep else ""))
    return 0


def cmd_wrap(args: argparse.Namespace) -> int:
    from . import wrapper

    project = _project(args)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("error: wrap needs the agent command after `--`", file=sys.stderr)
        return 2
    return wrapper.run(project, args.agent, command, llm_name=args.llm, keep_recent=args.keep_recent,
                       auto_compact=args.auto_compact)


def cmd_install_opencode(args: argparse.Namespace) -> int:
    """Install (or remove) the /compacter slash command for the opencode TUI."""
    import shutil

    cfg_dir = Path(args.config_dir).expanduser() if args.config_dir else Path.home() / ".config" / "opencode"
    dest = cfg_dir / "ctxpipe" / "compacter.mjs"
    tui_json = cfg_dir / "tui.json"
    spec = dest.resolve().as_uri()
    try:
        data = json.loads(tui_json.read_text(encoding="utf-8")) if tui_json.exists() else {}
    except ValueError:
        print(f"{tui_json} has comments/invalid JSON; add this to it by hand:")
        print(f'  "plugin": ["{spec}"]')
        return 1
    plugins = [p for p in data.get("plugin", []) if p != spec]
    if args.uninstall:
        data["plugin"] = plugins
        if not plugins:
            data.pop("plugin")
        dest.unlink(missing_ok=True)
        print(f"removed {dest} and its entry in {tui_json}")
    else:
        src = Path(__file__).parent / "integrations" / "opencode_tui.mjs"
        dest.parent.mkdir(parents=True, exist_ok=True)
        exe = shutil.which("ctxpipe")
        cmd = [Path(exe).as_posix()] if exe else [Path(sys.executable).as_posix(), "-m", "ctxpipe"]
        code = src.read_text(encoding="utf-8").replace(': ["ctxpipe"] // CTXPIPE_CMD', f": {json.dumps(cmd)} // CTXPIPE_CMD")
        dest.write_text(code, encoding="utf-8")
        data.setdefault("$schema", "https://opencode.ai/tui.json")
        data["plugin"] = [*plugins, spec]
        print(f"installed {dest}")
        print(f"registered in {tui_json}")
        print("restart opencode, then type /compacter")
    tui_json.parent.mkdir(parents=True, exist_ok=True)
    tui_json.write_text(json.dumps(data, indent=2) + os.linesep, encoding="utf-8")
    return 0


def _hook_command(agent: str) -> str:
    import shutil

    exe = shutil.which("ctxpipe")
    base = f'"{Path(exe).as_posix()}"' if exe else f'"{Path(sys.executable).as_posix()}" -m ctxpipe'
    return f"{base} hook --agent {agent}"


FALLBACK_COMMAND = """---
description: Choose which messages to compact (ctxpipe)
---
The ctxpipe hook normally handles this command before it reaches you. If you are reading this,
it is not installed: run `ctxpipe install-claude-code` in a terminal, then retry.
"""


def cmd_install_claude_code(args: argparse.Namespace) -> int:
    """Register the /compacter prompt hook in ~/.claude/settings.json."""
    import shutil

    home = Path(args.config_dir).expanduser() if args.config_dir else Path.home() / ".claude"
    path = home / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        print(f"{path} is not plain JSON; add a UserPromptSubmit hook running:")
        print(f"  {_hook_command('claude-code')}")
        return 1
    groups = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    mine = lambda g: any(" hook --agent claude-code" in h.get("command", "") and "ctxpipe" in h.get("command", "")
                         for h in g.get("hooks", []))
    groups[:] = [g for g in groups if not mine(g)]
    cmd_file = home / "commands" / "compacter.md"
    if args.uninstall:
        cmd_file.unlink(missing_ok=True)
        print(f"removed the /compacter hook from {path}")
    else:
        groups.append({"hooks": [{"type": "command", "command": _hook_command("claude-code"), "timeout": 180}]})
        cmd_file.parent.mkdir(parents=True, exist_ok=True)
        cmd_file.write_text(FALLBACK_COMMAND, encoding="utf-8")
        print(f"registered the /compacter hook in {path}")
        print("restart Claude Code, then type /compacter")
    if not groups:
        data["hooks"].pop("UserPromptSubmit")
    if not data["hooks"]:
        data.pop("hooks")
    if path.exists():
        shutil.copyfile(path, path.with_suffix(".json.ctxpipe-bak"))
    home.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + os.linesep, encoding="utf-8")
    return 0


def cmd_install_codex(args: argparse.Namespace) -> int:
    """Print the Codex hook block (not auto-written: untested against your Codex build)."""
    cmd = _hook_command("codex").replace("\\", "/")
    print("Add this to ~/.codex/config.toml (Codex hooks), then restart Codex:")
    print()
    print("[[hooks.UserPromptSubmit]]")
    print("[[hooks.UserPromptSubmit.hooks]]")
    print('type = "command"')
    print(f"command = '{cmd}'")
    print("timeout = 180")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    from . import hook

    return hook.run(args.agent, keep_recent=args.keep_recent, llm_name=args.llm)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ctxpipe", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--project", default=".", help="project directory (default: .)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("start", help="import an agent session into the master transcript")
    sp.add_argument("--agent", required=True)
    sp.set_defaults(fn=cmd_start)

    sub.add_parser("status", help="list messages with tags/policies").set_defaults(fn=cmd_status)

    sp = sub.add_parser("tag", help="tag a message")
    sp.add_argument("target", help="'last', index, or id prefix")
    sp.add_argument("--as", dest="as_tag", required=True)
    sp.set_defaults(fn=cmd_tag)

    sp = sub.add_parser("mark", help="set a message policy")
    sp.add_argument("target")
    sp.add_argument("--policy", required=True, choices=POLICIES)
    sp.set_defaults(fn=cmd_mark)

    sp = sub.add_parser("drop-last", help="mark the last N messages drop_always")
    sp.add_argument("n", type=int)
    sp.set_defaults(fn=cmd_drop_last)

    sp = sub.add_parser("compact", help="compact the session")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--preview", action="store_true", help="show plan + summary (default)")
    g.add_argument("--apply", action="store_true", help="write compacted session via adapter")
    sp.add_argument("--keep-recent", type=int, default=None,
                    help="recent N turns kept verbatim (profile default when omitted)")
    sp.add_argument("--target-tokens", type=int, default=1500)
    sp.add_argument("--threshold", type=int, default=0,
                    help="skip compaction when visible tokens are at or below this")
    sp.add_argument("--profile", default="balanced",
                    choices=["balanced", "conservative", "aggressive", "code-heavy"])
    sp.add_argument("--auto", action="store_true", help="run automatic compaction flow")
    sp.add_argument("--checkpoint", action="store_true", help="print continuity checkpoint JSON")
    sp.add_argument("--verify", action="store_true", help="verify transcript integrity + essences")
    sp.add_argument("--apply-if-valid", action="store_true",
                    help="automatic compaction only when verification passes (never asks)")
    sp.add_argument("--review", choices=["summary", "items", "diff"], default=None)
    sp.add_argument("--select", action="store_true",
                    help="interactive checklist: tick the messages to compact; the rest stay verbatim")
    sp.add_argument("--only", default=None, metavar="RANGES",
                    help="compact only these message indexes, e.g. 0,3-5,-2 (others stay verbatim)")
    sp.add_argument("--only-ids", default=None, metavar="IDS",
                    help="compact only these message ids (ctxpipe or agent ids, comma/space "
                         "separated; '-' reads them from stdin). Used by the opencode plugin")
    sp.add_argument("--llm", default="anthropic")
    sp.set_defaults(fn=cmd_compact)

    sp = sub.add_parser("auto-tag", help="suggest (or apply) tags from message patterns")
    sp.add_argument("--apply", action="store_true", help="write suggested tags to the master transcript")
    sp.add_argument("--min-confidence", type=float, default=None)
    sp.add_argument("--also-set-keep-verbatim", action="store_true",
                    help="also set keep_verbatim on goal/constraint suggestions")
    sp.set_defaults(fn=cmd_auto_tag)

    sp = sub.add_parser("eval", help="LLM-as-judge: does the compacted context still support the task?")
    sp.add_argument("--task", required=True, help="the task the agent was working on")
    sp.add_argument("--keep-recent", type=int, default=6)
    sp.add_argument("--target-tokens", type=int, default=1500)
    sp.add_argument("--llm", default="anthropic")
    sp.set_defaults(fn=cmd_eval)

    sp = sub.add_parser("wrap", help="launch the agent with stdio passthrough; /compact is intercepted")
    sp.add_argument("--agent", required=True)
    sp.add_argument("--keep-recent", type=int, default=6)
    sp.add_argument("--llm", default="anthropic")
    sp.add_argument("--auto-compact", nargs="?", const="config", default="off",
                    choices=["config", "ask", "apply", "off"],
                    help="automatic compaction: bare flag=config, or ask/apply/off")
    sp.add_argument("command", nargs=argparse.REMAINDER, help="agent command after `--`")
    sp.set_defaults(fn=cmd_wrap)

    sp = sub.add_parser("install-opencode", help="add the /compacter slash command to opencode")
    sp.add_argument("--uninstall", action="store_true")
    sp.add_argument("--config-dir", default=None, help="opencode config dir (default ~/.config/opencode)")
    sp.set_defaults(fn=cmd_install_opencode)

    sp = sub.add_parser("install-claude-code", help="add the /compacter slash command to Claude Code")
    sp.add_argument("--uninstall", action="store_true")
    sp.add_argument("--config-dir", default=None, help="Claude config dir (default ~/.claude)")
    sp.set_defaults(fn=cmd_install_claude_code)

    sp = sub.add_parser("install-codex", help="print the /compacter hook block for Codex")
    sp.set_defaults(fn=cmd_install_codex)

    sp = sub.add_parser("hook", help="prompt-hook entry point (JSON on stdin); used by the agents")
    sp.add_argument("--agent", required=True, choices=["claude-code", "codex"])
    sp.add_argument("--keep-recent", type=int, default=6)
    sp.add_argument("--llm", default="anthropic")
    sp.set_defaults(fn=cmd_hook)

    acp = sub.add_parser("auto-compact", help="manage automatic compaction")
    acpsub = acp.add_subparsers(dest="acmd", required=True)
    acpsub.add_parser("status", help="show auto-compaction status").set_defaults(fn=cmd_ac_status)
    acpsub.add_parser("enable", help="enable in ctxpipe.yaml").set_defaults(fn=cmd_ac_enable)
    acpsub.add_parser("disable", help="disable in ctxpipe.yaml").set_defaults(fn=cmd_ac_disable)
    acpsub.add_parser("assess", help="print a compactability assessment").set_defaults(fn=cmd_ac_assess)
    acpsub.add_parser("reset-cooldown", help="clear cooldown metadata").set_defaults(fn=cmd_ac_reset)
    acpsub.add_parser("rollback", help="restore the transcript from before the last automatic compaction"
                      ).set_defaults(fn=cmd_ac_rollback)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, LookupError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

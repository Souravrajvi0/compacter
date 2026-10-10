"""Benchmarks: policy-aware compaction vs naive compaction.

Naive compaction (what an agent does by itself): no tags, no policies, no
pair safety — summarize everything older than `keep_recent`, keep the recent
window. codec: pins tagged messages, honors drop_always/drop_ok, keeps
tool pairs together, truncates successful tool results, drops them when
allowed.

Fixtures + synthetic tasks. Output: benchmarks/results/benchmark.json +
benchmarks/results/BENCHMARKS.md.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from codec import evals  # noqa: E402
from codec.config import ScopeRule  # noqa: E402
from codec.core import compaction, store  # noqa: E402
from codec.core.models import Message, Policy, Rule, Session  # noqa: E402
from codec.core.summary import parse_structured_summary  # noqa: E402
from codec.core.tokens import estimate_tokens, session_tokens  # noqa: E402

FIXTURES = ROOT / "evals" / "fixtures"
EXAMPLES = ROOT / "examples" / "sessions"
OUT = ROOT / "benchmarks" / "results"

SECRET_MARKERS = ("sk-live", "sk-secret", "api key")
TOOL_SCOPE = [ScopeRule("tool_result", Rule(drop_result_if="no_errors", truncate_result_to=30))]
PYTHON = sys.executable


def load(path: Path) -> Session:
    tmp = Path(tempfile.mkdtemp())
    try:
        d = tmp / ".codec"
        d.mkdir()
        shutil.copy(path, d / "session.jsonl")
        return store.load(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def naive_compact(s: Session, keep_recent: int) -> Session:
    older, recent = s.messages[:-keep_recent], s.messages[-keep_recent:]
    summary_text = " ".join(m.content for m in older)[: 4 * 1500]
    out = Session(agent=s.agent, project=s.project, system=s.system,
                  tool_schemas=s.tool_schemas, meta=dict(s.meta))
    out.messages.append(Message("user", "[summary of earlier conversation]\n" + summary_text))
    out.messages.extend(recent)
    return out


class JSONStubLLM:
    """Deterministic structured summarizer; output varies by prompt profile."""

    def complete(self, system, user, max_tokens=2048):
        blocks = re.findall(r'<message id="[^"]+" role="[^"]+" tags="([^"]*)"[^>]*>\n(.*?)\n</message>', user, re.S)
        goals, constraints, decisions, others = [], [], [], []
        for tags, content in blocks:
            tags = set(tags.split(",")) if tags else set()
            if "goal" in tags:
                goals.append(content.strip())
            elif "constraint" in tags:
                constraints.append(content.strip())
            elif "decision" in tags:
                decisions.append({"what": content.strip(), "why": "", "alternatives_rejected": []})
            else:
                others.append(content.strip())
        medium = "; ".join(others)[:400] if others else ""
        low = "; ".join(others)[:2000] if others else ""
        if "aggressive" in system:
            medium = medium[:120]
            low = ""
        if "code-heavy" in system:
            medium = medium[:800]
        return json.dumps({
            "summary_high": {"goals": goals, "constraints": constraints, "decisions": decisions,
                             "open_tasks": [], "key_files": []},
            "summary_medium": medium,
            "summary_low": low,
        })


def codec_compact(s: Session, keep_recent: int, profile: str = "balanced") -> Session:
    p = compaction.plan(s, keep_recent=keep_recent, scopes=TOOL_SCOPE)
    previous = None
    for m in s.messages:
        if "compaction_summary" in m.tags and m.meta.get("summary_json"):
            try:
                previous = json.loads(m.meta["summary_json"])
            except ValueError:
                pass
    summary = compaction.summarize(p, JSONStubLLM(), previous_summary=previous,
                                   profile=profile, scopes=TOOL_SCOPE)
    return compaction.rebuild(s, p, summary, scopes=TOOL_SCOPE)


class JudgeStub:
    def complete(self, system, user, max_tokens=2048):
        transcript = user.split("COMPACTED TRANSCRIPT:", 1)[-1]
        ok = ("tags=goal" in transcript or "tags=decision" in transcript) and "sk-live" not in transcript
        return ("YES\npinned tags survive, secrets excluded." if ok
                else "NO\npinned tags missing or secret leaked.")


def view_text(view: Session) -> str:
    return "\n".join(m.content for m in view.messages)


def pin_preserved(view: Session) -> bool:
    return any(m.policy is Policy.KEEP_VERBATIM or m.tags & {"goal", "constraint"} for m in view.messages)


def secret_leaked(view: Session) -> bool:
    return any(marker in view_text(view).lower() for marker in SECRET_MARKERS)


def pair_splits(view: Session) -> int:
    owners = [set(m.pair_ids()) for m in view.messages]
    flat = set().union(*owners) if owners else set()
    return sum(1 for pid in flat if sum(1 for ids in owners if pid in ids) == 1)


def measure(fixture: Path, keep_recent: int) -> dict:
    s = load(fixture)
    before = session_tokens(s.visible()) + estimate_tokens(s.system)
    row = {"fixture": fixture.name, "keep_recent": keep_recent, "tokens_before": before}
    for name, fn in (("naive", naive_compact), ("codec", codec_compact)):
        view = fn(s, keep_recent)
        row[name] = {
            "tokens_after": session_tokens(view.messages) + estimate_tokens(view.system),
            "pins_preserved": pin_preserved(view),
            "secret_leaked": secret_leaked(view),
            "pair_splits": pair_splits(view),
            "metrics": evals.compute_metrics(s, view),
        }
    return row


def tool_heavy_session() -> Session:
    s = Session(agent="generic-api", project="bench")
    msgs = [msg_user("build the thing", tags={"goal"}, policy=Policy.KEEP_VERBATIM)] if False else []
    msgs = [Message("user", "Goal: make the tool reliable", tags={"goal"}, policy=Policy.KEEP_VERBATIM)]
    for i in range(6):
        msgs.append(Message("assistant", f"running step {i}", meta={"pair_ids": [f"c{i}"]}))
        msgs.append(Message("tool", f"step {i} output ok " * 40, meta={"pair_ids": [f"c{i}"]},
                            rule=Rule(drop_result_if="no_errors", truncate_result_to=30)))
    msgs.append(Message("assistant", "running risky step", meta={"pair_ids": ["err1"]}))
    msgs.append(Message("tool", "Traceback: something broke in step 6", meta={"pair_ids": ["err1"]},
                        rule=Rule(drop_result_if="no_errors")))
    msgs.append(Message("user", "Constraint: errors must stay visible", tags={"constraint"}, policy=Policy.KEEP_VERBATIM))
    msgs.append(Message("assistant", "patched the bug, tests pass", policy=Policy.SUMMARIZE_OK))
    s.messages = msgs
    return s


def msg_user(content, tags=None, policy=None):
    return Message("user", content, tags=tags or set(), policy=policy or Policy.SUMMARIZE_OK)


def benchmark_tool_heavy() -> dict:
    s = tool_heavy_session()
    before = session_tokens(s.visible())
    view = codec_compact(s, keep_recent=3)
    after = session_tokens(view.messages)
    error_kept = any(m.role == "tool" and "Traceback" in m.content for m in view.messages)
    return {"fixture": "tool-heavy (synthetic)", "tokens_before": before, "tokens_after": after,
            "error_result_preserved": error_kept,
            "ok_results_dropped": sum(1 for m in s.messages if m.role == "tool" and "ok" in m.content)
            - sum(1 for m in view.messages if m.role == "tool" and "ok" in m.content),
            "metrics": evals.compute_metrics(s, view)}


def benchmark_chained() -> dict:
    s = load(FIXTURES / "session-a.jsonl")
    history: list[dict] = []
    current = s
    for round_ in range(3):
        view = codec_compact(current, keep_recent=4)
        summary_json = None
        for m in view.messages:
            if "compaction_summary" in m.tags and m.meta.get("summary_json"):
                summary_json = m.meta["summary_json"]
        data = parse_structured_summary(summary_json or "") if summary_json else None
        history.append({
            "round": round_ + 1,
            "valid_json": data is not None,
            "goals_in_high": len(data["summary_high"]["goals"]) if data else 0,
            "tokens_after": session_tokens(view.messages),
            "goals_header_count": view.messages[1].content.count("## Goals") if len(view.messages) > 1 else 0,
        })
        current = view
        current.messages.append(Message("user", f"follow-up {round_}: also remember the path src/x.py"))
        current.messages.append(Message("assistant", f"ack {round_}"))
    return {"fixture": "chained session-a (3 rounds)", "rounds": history}


def benchmark_profiles() -> list[dict]:
    rows = []
    for profile in ("conservative", "aggressive", "code-heavy"):
        s = load(FIXTURES / "session-a.jsonl")
        view = codec_compact(s, keep_recent=4, profile=profile)
        summary_json = ""
        for m in view.messages:
            if "compaction_summary" in m.tags:
                summary_json = m.meta.get("summary_json", "")
        data = parse_structured_summary(summary_json)
        rows.append({
            "profile": profile,
            "tokens_after": session_tokens(view.messages),
            "summary_high_chars": len(json.dumps(data["summary_high"])) if data else 0,
            "summary_medium_chars": len(data["summary_medium"]) if data else 0,
            "summary_low_chars": len(data["summary_low"]) if data else 0,
        })
    return rows


def main() -> None:
    rows = []
    for fixture in sorted(FIXTURES.glob("*.jsonl")) + sorted(EXAMPLES.glob("original.jsonl")):
        rows.append(measure(fixture, keep_recent=4))

    tool_row = benchmark_tool_heavy()
    chained = benchmark_chained()
    profiles = benchmark_profiles()

    OUT.mkdir(parents=True, exist_ok=True)
    payload = {"fixtures": rows, "tool_heavy": tool_row, "chained": chained, "profiles": profiles}
    (OUT / "benchmark.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = ["# Compaction benchmark results", "", "## Fixtures: naive vs codec", ""]
    lines.append("| fixture | strategy | tokens before -> after | pins preserved | secret leaked | pair splits |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        for name in ("naive", "codec"):
            m = r[name]
            lines.append(f"| {r['fixture']} | {name} | {r['tokens_before']} -> {m['tokens_after']} "
                         f"| {'yes' if m['pins_preserved'] else 'no'} "
                         f"| {'**YES**' if m['secret_leaked'] else 'no'} | {m['pair_splits']} |")
    lines += ["", "## Tool-heavy session (drop_result_if: no_errors + truncation)", ""]
    lines.append(f"- tokens: {tool_row['tokens_before']} -> {tool_row['tokens_after']}")
    lines.append(f"- error result preserved: {tool_row['error_result_preserved']}")
    lines.append(f"- successful results dropped: {tool_row['ok_results_dropped']}")
    lines += ["", "## Chained compaction (3 rounds on session-a)", ""]
    for h in chained["rounds"]:
        lines.append(f"- round {h['round']}: valid_json={h['valid_json']} goals_in_high={h['goals_in_high']} tokens_after={h['tokens_after']} goals_header_count={h['goals_header_count']}")
    lines += ["", "## Profile comparison (session-a, keep_recent=4)", ""]
    lines.append("| profile | tokens after | high chars | medium chars | low chars |")
    lines.append("|---|---|---|---|---|")
    for r in profiles:
        lines.append(f"| {r['profile']} | {r['tokens_after']} | {r['summary_high_chars']} | {r['summary_medium_chars']} | {r['summary_low_chars']} |")

    (OUT / "BENCHMARKS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines).encode("ascii", "replace").decode())
    print("\nwrote", OUT / "benchmark.json", "and", OUT / "BENCHMARKS.md")


if __name__ == "__main__":
    main()

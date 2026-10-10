"""Regenerate the demo artifacts in examples/.

Uses a deterministic stub LLM so the committed snapshots are stable.
With ANTHROPIC_API_KEY set, swap StubLLM for the real provider:
    from codec.llm import get_llm
    llm = get_llm("anthropic")
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from codec.core import compaction, store  # noqa: E402
from codec.core.models import Message, Policy, Session  # noqa: E402


class StubLLM:
    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str:
        n = user.count("<message id=")
        return f"## Progress\n- (stub summary) folded {n} messages into one block.\n- Goals/constraints remain pinned above."


def build_session() -> Session:
    s = Session(agent="generic-api", project=str(HERE))
    s.messages = [
        Message("user", "Build me a JSONL session compaction tool for CLI agents.",
                tags={"goal"}, policy=Policy.KEEP_VERBATIM),
        Message("user", "Constraint: never let a pasted secret reach the agent again.",
                tags={"constraint"}, policy=Policy.KEEP_VERBATIM),
        Message("assistant", "Scaffolded cli.py and core models."),
        Message("assistant", "[tool_result: pytest output, 19 passed...]" * 40,
                policy=Policy.DROP_OK),
        Message("user", "Here is my API key: sk-live-xyz-12345",
                policy=Policy.DROP_ALWAYS),
        Message("assistant", "Store layer done: session.jsonl with a header line."),
        Message("assistant", "[tool_result: 40-line traceback from failed run...]" * 20,
                policy=Policy.DROP_OK),
        Message("user", "Decision: compaction is preview-then-apply, never silent.",
                tags={"decision"}),
        Message("assistant", "Added eval judge: codec eval --task ..."),
        Message("user", "Now add an Aider adapter."),
        Message("assistant", "Aider adapter parses #### user / #### assistant blocks."),
        Message("user", "Wrap mode: /compact should be intercepted."),
    ]
    return s


def compact(s: Session, keep_recent: int) -> tuple[Session, compaction.CompactionPlan, str]:
    p = compaction.plan(s, keep_recent=keep_recent)
    summary = compaction.summarize(p, StubLLM())
    return compaction.rebuild(s, p, summary), p, summary


def main() -> None:
    sessions = HERE / "sessions"
    sessions.mkdir(exist_ok=True)

    s = build_session()
    store.save(s, HERE / ".demo")
    original = (HERE / ".demo" / ".codec" / "session.jsonl").read_text(encoding="utf-8")
    (sessions / "original.jsonl").write_text(original, encoding="utf-8")

    v1, p1, _ = compact(s, keep_recent=4)
    store.save(v1, HERE / ".demo1")
    (sessions / "compacted-1.jsonl").write_text(
        (HERE / ".demo1" / ".codec" / "session.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    v1.messages.append(Message("user", "Day 2: Codex sessions import works."))
    v1.messages.append(Message("assistant", "Rollout parser handles function_call blocks."))
    v2, p2, _ = compact(v1, keep_recent=3)
    store.save(v2, HERE / ".demo2")
    (sessions / "compacted-2.jsonl").write_text(
        (HERE / ".demo2" / ".codec" / "session.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    report = {
        "compaction_1": {"pinned": len(p1.pinned), "summarized": len(p1.to_summarize),
                         "dropped": len(p1.dropped), "excluded": len(p1.excluded)},
        "compaction_2": {"pinned": len(p2.pinned), "summarized": len(p2.to_summarize),
                         "dropped": len(p2.dropped), "excluded": len(p2.excluded)},
    }
    (sessions / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for d in (".demo", ".demo1", ".demo2"):
        shutil.rmtree(HERE / d, ignore_errors=True)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

"""Three automatic-compaction workflows, runnable offline (deterministic stub LLM).

    python examples/auto_compaction_demo.py

The same code paths run with a real LLM; only the summarizer differs.
Output is pasted into docs/RECIPES.md.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "src"))

from compacter.core import compaction, store  # noqa: E402
from compacter.core.autocontrol import AutoCompactConfig, AutoCompactController, capture_checkpoint  # noqa: E402
from compacter.core.tokens import estimate_tokens, session_tokens  # noqa: E402


class StubLLM:
    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str:
        return json.dumps({
            "summary_high": {
                "goals": ["Build me a JSONL session compaction tool for CLI agents."],
                "constraints": ["Constraint: never let a pasted secret reach the agent again."],
                "decisions": [], "open_tasks": [], "key_files": []},
            "summary_medium": "Explored adapters and wrote the core engine.", "summary_low": ""})


class ScriptedUI:
    """Echoes the prompt and the scripted answer, like a terminal session."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)

    def ask(self, text: str) -> str:
        print(text, end="")
        ans = self.answers.pop(0) if self.answers else ""
        print(ans)
        return ans

    def info(self, text: str) -> None:
        print(text)


def load_session() -> tuple[Path, object]:
    tmp = Path(tempfile.mkdtemp(prefix="compacter-auto-"))
    (tmp / ".compacter").mkdir()
    shutil.copy(HERE / "sessions" / "original.jsonl", tmp / ".compacter" / "session.jsonl")
    return tmp, store.load(tmp)


def scenario(title: str, ratio: float, answers: list[str] | None, interactive: bool, **cfg_kw) -> None:
    print(f"\n=== {title} ===")
    project, s = load_session()
    cfg = AutoCompactConfig(enabled=True, reserve_output_tokens=0, reserve_tool_tokens=0,
                            min_turns_since_compaction=0, min_new_tokens=0, cooldown_turns=0,
                            min_projected_reduction=0.2, min_reduction=0.2, keep_recent=3, **cfg_kw)
    cfg.context_window = int((session_tokens(s.visible()) + estimate_tokens(s.system)) / ratio)
    llm = StubLLM()

    def apply_fn(session):
        p = compaction.plan(session, keep_recent=cfg.keep_recent)
        return compaction.rebuild(session, p, compaction.summarize(p, llm))

    before = session_tokens(s.visible())
    ctrl = AutoCompactController(s, cfg)
    ckpt = {**capture_checkpoint(s), "next_action": "write the README"}
    result = ctrl.evaluate(checkpoint=ckpt, interactive=interactive, apply_fn=apply_fn,
                           ui=ScriptedUI(answers) if answers is not None else None)
    a = result.get("assessment", {})
    print(f"-> action={result['action']}  pressure={result.get('pressure', 0):.0%}  "
          f"risk={a.get('semantic_risk')}  recommendation={a.get('recommendation')}")
    if result["action"] == "applied":
        print(f"-> tokens {before} -> {session_tokens(s.visible())}; checkpoint kept; "
              f"rollback available: {'rollback' in s.meta['auto_compaction']}")
    shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    scenario("1. Low-risk automatic application (ask_user: never, 80% full)", 0.80, None, False, ask_user="never")
    scenario("2. Essence prompt (ask_user: always; ask_user: smart asks the same when risk is medium or higher)", 0.80,
             ["e", "keep the exact adapter names and the rejected regex parser", "a"], True, ask_user="always")
    scenario("3. Emergency compaction (97% full: never asks, applies only if verified)", 0.97, None, True,
             ask_user="always")

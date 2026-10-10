"""Run the canonical eval tasks against the compaction engine + LLM judge.

Usage:
    python evals/run_evals.py            # auto: Anthropic if key set, else stub
    python evals/run_evals.py --stub     # force the deterministic stub judge

Writes evals/results/<timestamp>.json with one entry per task.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from compacter import evals  # noqa: E402
from compacter.core import compaction, store  # noqa: E402

FIXTURES = ROOT / "evals" / "fixtures"
RESULTS = ROOT / "evals" / "results"


class StubLLM:
    """Deterministic stand-in when no ANTHROPIC_API_KEY is available."""

    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str:
        if "everything needed to complete the given task" in system:
            transcript = user.split("COMPACTED TRANSCRIPT:", 1)[-1]
            ok = ("tags=goal" in transcript or "tags=decision" in transcript) and "sk-live" not in transcript
            return ("YES\ntagged goal/decision messages survive verbatim; drop_always content is absent." if ok
                    else "NO\nmissing pinned tags or drop_always content leaked.")
        return "## Progress\n- (stub summary) folded earlier messages."


def _load_session_from(path: Path):
    tmp = Path(tempfile.mkdtemp())
    target = tmp / ".compacter"
    target.mkdir()
    shutil.copy(path, target / "session.jsonl")
    try:
        return store.load(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stub", action="store_true")
    args = ap.parse_args()

    if args.stub:
        llm = StubLLM()
        provider = "stub"
    else:
        import os

        if os.environ.get("ANTHROPIC_API_KEY"):
            from compacter.llm import get_llm

            llm = get_llm("anthropic")
            provider = "anthropic"
        else:
            print("ANTHROPIC_API_KEY not set; using stub judge (STUB results)")
            llm = StubLLM()
            provider = "stub"

    tasks = json.loads((ROOT / "evals" / "tasks.json").read_text(encoding="utf-8"))
    RESULTS.mkdir(exist_ok=True)
    out = []
    for t in tasks["tasks"]:
        session = _load_session_from(FIXTURES / t["session"])
        p = compaction.plan(session, keep_recent=t.get("keep_recent", 4))
        summary = compaction.summarize(p, llm)
        view = compaction.rebuild(session, p, summary)
        result = evals.judge(view, t["task"], llm, original=session)
        out.append({"task": t["name"], "passed": result.passed,
                    "explanation": result.explanation, "provider": provider,
                    "metrics": result.metrics, "expected": t.get("expected", "")})
        print(("PASS" if result.passed else "FAIL"), "-", t["name"], "-", result.explanation)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS / f"{stamp}.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("wrote", path)
    return 0 if all(r["passed"] for r in out) else 2


if __name__ == "__main__":
    raise SystemExit(main())

# Evals

Canonical tasks live in `tasks.json`. Each bundles an input session fixture
(`fixtures/*.jsonl`), a natural-language task, a `keep_recent`, and the
expected judge verdict. The judge is LLM-as-judge: it sees the task and the
compacted view, and must answer YES/NO + one sentence.

## Running

```bash
python evals/run_evals.py            # Anthropic if ANTHROPIC_API_KEY is set
python evals/run_evals.py --stub     # deterministic offline judge
```

Results are appended to `results/<timestamp>.json`.

## Tasks

1. **compress tool-heavy session, keep constraints and goals** — fixture:
   `session-a.jsonl` (from `examples/`). Expected: PASS.
2. **drop experimental branches, preserve decisions and file paths** —
   fixture: `session-b.jsonl`. Expected: PASS.

## Sample output (stub judge, offline)

```
PASS - compress tool-heavy session, keep constraints and goals - tagged goal/decision messages survive verbatim; drop_always content is absent.
PASS - drop experimental branches, preserve decisions and file paths - tagged goal/decision messages survive verbatim; drop_always content is absent.
```

## Known failure modes

- Over-summarization: if `keep_recent` is too small or a critical message was
  left `summarize_ok`, the judge should FAIL — tag such messages
  `goal`/`constraint` or mark `keep_verbatim` and re-run.
- Pair splitting used to show up here as "tool result without its call";
  compaction now keeps pairs together (see `core/compaction.py`).
- The stub judge only checks for pinned tags and absent secrets; it is **not**
  evidence of quality. Use the Anthropic provider for real verdicts.

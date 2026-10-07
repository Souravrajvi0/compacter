# Demo: two chained compactions

Artifacts in `sessions/`:

| File | What it is |
|---|---|
| `original.jsonl` | Master transcript after `start` + tagging (12 messages, ~5.1 KB, includes a huge fake tool log and a pasted secret) |
| `compacted-1.jsonl` | View after the first `compact --apply` (keep-recent 4) |
| `compacted-2.jsonl` | After two more messages and a second `compact --apply` (keep-recent 3) |
| `report.json` | Bucket counts per compaction |

## What was tagged and why

- Message 0 (`goal`, `keep_verbatim`): the task statement must survive every compaction.
- Message 1 (`constraint`, `keep_verbatim`): the safety rule must never be lost.
- Message 4 (`sk-live-...`, `drop_always`): a pasted secret — never visible to the agent again.
- Messages 3 and 6 (`drop_ok`): huge tool logs, safe to discard outright.
- Message 7 (`decision`): tagged so it can be found, still summarized if old.

## What changed

`report.json`:

```json
{"compaction_1": {"pinned": 2, "summarized": 3, "dropped": 2, "excluded": 1},
 "compaction_2": {"pinned": 2, "summarized": 4, "dropped": 0, "excluded": 0}}
```

- Compaction 1: pinned the goal + constraint verbatim, folded 3 summarizable
  messages into one summary block, dropped the two huge logs, and excluded the
  secret from the agent's view entirely.
- Compaction 2: the previous summary, plus the two new day-2 messages from the
  agent, get folded into a single new summary. The goal and constraint are
  re-derived from the fresh master and stay verbatim.

## Eval verdict

```bash
ctxpipe eval --task "preserve the goal and the no-secrets constraint"
```

With the real LLM judge this prints `PASS - goal and constraint survive as
pinned, verbatim messages; untagged noise was folded or dropped.` A FAIL here
is the signal to loosen `keep_recent` or pin more context with tags.

## Regenerating

```bash
python examples/make_demo.py
```

The committed snapshots use a deterministic stub summarizer so they diff
cleanly. Set `ANTHROPIC_API_KEY` and swap the `StubLLM` for the real provider
(see the comment in `make_demo.py`) to get LLM-phrased summaries.

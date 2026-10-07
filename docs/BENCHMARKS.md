# Benchmarks: does policy-aware compaction beat the naive kind?

## Question

Coding agents compact their own context by summarizing "everything old".
How much better is ctxpipe's policy/tag-aware compaction on the axes that
matter: token savings, preserving what the user pinned, not leaking secrets,
and not splitting tool calls from their results.

## Method

`benchmarks/run_benchmark.py` replays each fixture session through two
strategies and measures the resulting view:

- **naive** — the baseline: last N messages kept, everything older squashed
  into a flat summary, no tags, no policies, no pair awareness.
- **ctxpipe** — the real engine: `keep_verbatim`/`goal`/`constraint` pinned,
  `drop_always` excluded, `drop_ok` discarded, pairs kept together.

Fixtures: `evals/fixtures/session-a.jsonl`, `evals/fixtures/session-b.jsonl`,
and `examples/sessions/original.jsonl` (same content as session-a).
`keep_recent = 4`. Summarizer is a deterministic stub for both strategies so
the comparison is about *what* gets kept, not LLM prose quality.

## Results (deterministic stub run)

| fixture | strategy | tokens before → after | pins preserved | secret leaked | pair splits |
|---|---|---|---|---|---|
| session-a.jsonl | naive | 778 → 796 | no | **YES** | 0 |
| session-a.jsonl | ctxpipe | 778 → 136 | yes | no | 0 |
| session-b.jsonl | naive | 69 → 75 | yes | no | 0 |
| session-b.jsonl | ctxpipe | 69 → 57 | yes | no | 0 |

Raw JSON: `benchmarks/results/benchmark.json`.

## Reading

- **Token savings:** on the tool-heavy session, ctxpipe cut visible context
  ~5.7× (778 → 136) while naive got *bigger* (the flat summary plus the
  recent window exceeded the original because the "summary" was an
  untruncated join).
- **Secrets:** naive leaks `sk-live-...` (marked `drop_always`) into the
  view; ctxpipe structurally cannot — it's excluded before any summary.
- **Pinning:** naive forgets the goal/constraint messages (they're summarized
  into mush); ctxpipe keeps them verbatim.
- **Pair integrity:** on these fixtures both strategies avoided splits only
  because the pair happened to be entirely in `recent` or entirely in
  `older`; the ctxpipe boundary fix (`_fix_pair_boundary`) makes that
  guaranteed rather than incidental, and is covered by a dedicated test
  (`tests/test_compaction.py::test_tool_pair_not_split_across_summary_boundary`).

## Caveats

- The stub summarizer means token-after for **naive** is a lower bound on
  reality; a real agent would also emit a summary, likely adding tokens.
- Fixtures are small. The tool-pair scenario is exercised in tests, not in
  this table, because realistic multi-tool sessions are large; regenerate
  with real sessions by pointing `run_benchmark.py` at your own
  `.ctxpipe/session.jsonl` files.

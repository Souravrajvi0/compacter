# Roadmap

1. [x] Models, store, compaction engine, CLI skeleton, generic adapter
2. [x] Verify Claude Code session schema on a real session (works: 118 msgs); `adapters/claude_code.py`
3. [x] Handle tool_use/tool_result pairing (don't split a pair across summary/recent)
4. [x] Real tokenizer + `--threshold` auto-compaction
5. [x] Persist compaction into the master transcript (chained compactions)
6. [x] Live wrapper mode (`ctxpipe wrap --agent X -- <cmd>`, `/compact` interception)
7. [x] Adapters: Aider (`.aider.chat.history.md`), Codex (verified on real sessions)
8. [x] Quality evals: `ctxpipe eval --task ...` LLM-as-judge on the compacted view
9. [x] Benchmarks vs naive compaction + write-up (`docs/BENCHMARKS.md`)
10. [x] v2.0: scoped policies, structured layered summaries, auto-tag, profiles
11. [x] v2.1: opt-in automatic compaction (assessment, verification, circuit breakers, essence directives)

12. [x] v2.1.1: auto-compaction hardening (cooldown/rearm/later fixes, wrapper prompt, checkpoint, verification, review, rollback)

## Next
- [ ] Benchmark auto-compaction trigger quality on real long sessions
- [ ] Per-adapter checkpoint extraction (structured tool metadata instead of text heuristics)
- [ ] Publish to PyPI

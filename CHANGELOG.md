# Changelog

## Unreleased

- Context packs (`ctxpipe pack`, `ctxpipe new`), `ctxpipe carry`, and `--llm auto` (summarize with the agent CLI you already use)
- Per-version compatibility tests (`tests/test_compat.py`, fixtures in `tests/data/compat/`)
- `ctxpipe doctor`: compares installed claude/codex/opencode versions with the verified ones

## v2.1.1 – Auto-compaction hardening

Fixes
- Cooldown never expired: after one automatic compaction every later assessment was skipped forever
- `rearm_below_percent` is now enforced (hysteresis; lifted at the recommend threshold so it can never wedge)
- "Later" (`L`) and `default_action` are honored; empty answer / EOF follows `default_action`
- `ctxpipe wrap --auto-compact=ask` now actually prompts (UI + checkpoint were never passed);
  non-interactive `apply-if-valid` has a compactor
- Audit entries no longer contain essence text (directives get a hashed id); `user_action` is the real
  UI verdict (null when nobody was asked); duplicate audit append on chained compactions removed

Features
- Checkpoint extraction: `action_permission`, `files_modified_by_agent/user`, `pending_steps`, `next_action`
- Continuity checkpoint is inserted into the candidate, verified, and restored by repair; replaced (not accumulated) on each compaction
- New verification checks: exact facts, unsupported goals/constraints/decisions, specific completed-task regression
- Assessment: immediate-retrigger and protected-budget blockers, earlier failures, natural task boundary, recommend-threshold reason
- Interactive `[R]` review (Summary / Items / structured Diff); `compact --review` uses the same views
- Rollback snapshot + `ctxpipe auto-compact rollback`; atomic transcript writes
- One-time warning when pressure hits the force threshold after a postpone
- `history` of non-applied outcomes (later / disabled / rejected / circuit breaker)
- `examples/configs/ctxpipe-auto-compaction.yaml`, `examples/auto_compaction_demo.py`, docs in `docs/RECIPES.md`

## v2.1.0 – Automatic compaction

- Opt-in pressure-based auto-compaction (`auto_compaction:` in `ctxpipe.yaml`):
  evaluate / recommend / force thresholds on effective context pressure
- Compactability assessment with semantic-risk scoring and safe-boundary detection
- Verification + one-shot deterministic repair before applying; rollback on failure
- Circuit breakers, cooldown/hysteresis, postpone-by-turns/tokens, per-session disable
- Essence directives ("what must survive") with expiry after a successful apply
- Continuity checkpoint capture; audit entries in `meta.compactions`
- CLI: `auto-compact status|enable|disable|assess|reset-cooldown`,
  `compact --auto|--apply-if-valid|--checkpoint|--verify`, `wrap --auto-compact`
- New `AutoCompactConfig.keep_recent`; safe-boundary check no longer blocks on
  "pending steps while testing" (risk scoring prompts instead)
- Claude Code adapter schema re-verified against real sessions

## v2.0.0 – Advanced compaction rules & structured summaries

Step-change in compaction quality:

- `Rule` dataclass + per-message rules (`keep_verbatim_until`, `drop_ok_after`, `summarize_detail`, `truncate_result_to`, `keep_result_sections`, `drop_result_if`)
- Scoped policies via `ctxpipe.yaml` (`file:` / `tag:` / `role:` / `tool_result`) — most-specific wins, strictest breaks ties, per-message rule overrides
- Structured layered summaries `summary_high` / `summary_medium` / `summary_low`; medium/low degrade first under token pressure
- Chained compactions merge the previous structured summary (no more opaque re-summarization of it)
- Tool results: truncation, section filtering, `drop_result_if: no_errors`; errors always preserved at higher detail
- `ctxpipe auto-tag` heuristic tagger
- `--profile conservative|aggressive|code-heavy`
- Structured eval metrics (goals/constraints/decisions retained, errors kept verbatim, token reduction)
- Ready-to-copy configs in `examples/configs/`, recipes in `docs/RECIPES.md`

## 0.1.0

MVP release.

- Core: models (`Policy`, `Message`, `Session`), JSONL master transcript store,
  policy-aware compaction engine (plan → summarize → rebuild)
- CLI: `start`, `status`, `tag`, `mark`, `drop-last`, `compact --preview|--apply`,
  `eval`, `wrap`
- Compaction keeps `tool_use`/`tool_result` pairs together across the
  summary/recent boundary, and `--apply` chains compactions into the master
  transcript with a `meta.compactions` audit log
- Token counting via tiktoken (`cl100k_base`) with heuristic fallback;
  `--threshold` skips unnecessary compactions
- Adapters: Claude Code, Codex, Aider, generic JSON — all write new files
  instead of overwriting agent sessions
- Live wrapper mode: `ctxpipe wrap` intercepts `/compact`
- LLM-as-judge evals with canonical tasks in `evals/`
- Demo repo with chained-compaction snapshots in `examples/`

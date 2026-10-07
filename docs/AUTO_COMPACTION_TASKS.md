# Auto-compaction: work breakdown (ALL TASKS COMPLETE)

> Status: tasks 1-14 implemented; tests in `tests/test_auto_compaction.py` and `tests/test_auto_compaction_v2.py`
> (116 passing). Deviations from the text below: Task 2 re-arm is lifted at `recommend_at_percent` so it can never
> block forever; Task 7 compares summary claims against the *union* of original words (a single-message match
> rejected honest paraphrases); the essence directive audit stores a hashed `id`, never the text.

Audience: any coding model, including small ones. Do the tasks **in order**.
Each task is self-contained: it names the file, the exact change, and an
acceptance check you can run. Do not start a task until its `Needs:` tasks pass.

## Rules for the implementing model

1. One task at a time. After each task run `python -m pytest -q` — all tests
   must pass (baseline: **81 passed**) before moving on.
2. Add the tests named in the task to `tests/test_auto_compaction.py` (use the
   helpers already there: `mkcfg`, `big_session`, `sized`, `candidate_from`).
   Test first, watch it fail, then fix.
3. Do not rename or remove existing functions, config keys or CLI flags.
   New config fields get defaults so old `ctxpipe.yaml` files still load.
4. No new dependencies, no TUI/GUI, no network/LLM calls in unit tests.
5. Main code: `src/ctxpipe/core/autocontrol.py` (config, state machine,
   assess, checkpoint, essence, verify, repair, `AutoCompactController`).
   CLI: `src/ctxpipe/cli.py`. Live wrapper: `src/ctxpipe/wrapper.py`.
6. Pressure test trick: `sized(s, cfg, 0.8)` sets the context window so
   pressure ≈ 80%. Use 0.8 for "normal", 0.95 for "emergency" (≥ force 90%;
   emergency never asks the user and ignores postpone — by design).

## Status of the original spec (verified against the code)

| Area | Status |
|---|---|
| Config + validation (thresholds, `keep_recent`) | DONE |
| State machine + transitions + persistence in `s.meta["auto_compaction"]` | DONE |
| Effective pressure, `CompactionAssessment`, deterministic heuristics | DONE (gaps: Task 9) |
| Safe-boundary detection (open tool pair, risky phase) | DONE |
| Essence directives (create / expire on success / survive rejection) | DONE |
| Verification + repair-once + circuit breaker | DONE (gaps: Tasks 6, 7) |
| `always` / `smart` / `never` / `critical-only` ask modes | DONE |
| Prompt `[A][E][R][L][N]`, `L 10`, `L 15000t`, `keep ...; apply` parsing | DONE |
| CLI `auto-compact status/enable/disable/assess/reset-cooldown`, `compact --auto/--checkpoint/--verify/--apply-if-valid` | DONE |
| Audit entry with `automatic`, pressure, assessment, verification | DONE (gaps: Task 8) |
| 35 spec'd tests | DONE (all pass) |
| **Cooldown never expires** (bug) | DONE – Task 1** |
| **`rearm_below_percent` never used** | DONE – Task 2** |
| **"Later" postpone-by-default not honored** | DONE – Task 3** |
| **Wrapper never shows the prompt / passes no checkpoint** | DONE – Task 4** |
| Checkpoint fields `action_permission`, `files_modified_*` always empty | DONE – Task 5 |
| Checkpoint must survive compaction and be verified | DONE – Task 6 |
| Missing verification checks (exact facts, unsupported claims) | DONE – Task 7 |
| Audit gaps (`continuity_checkpoint_id`, later/disabled records) | DONE – Task 8 |
| Missing assessment inputs (retrigger, protected budget, failures) | DONE – Task 9 |
| Interactive `[R]` review views (Summary / Items / Diff) | DONE – Task 10 |
| Rollback snapshot + atomic save | DONE – Task 11 |
| Force-threshold re-warn after postpone; noninteractive critical | DONE – Task 12 |
| Example yaml, docs workflows, benchmark/example regression run | DONE – Tasks 13–14 |

## Dependency chain

```
1 → 2 → 3 → 4 → 12
5 → 6 → 7 → 8
9 (independent)    10 (needs 6)    11 (needs 8)
13, 14 last (need everything)
```

---

## Task 1 — Fix cooldown that never expires (BUG)

**Why:** `note_success` sets `meta["cooldown_turns_left"] = cooldown_turns`; `_skip_reason`
(used by `assess`) returns "still inside cooldown" while it is `> 0`, and nothing ever
decrements it, so after one compaction every later assessment is `skip` forever.

**Change:** in `_skip_reason`, remove the `cooldown_turns_left` check. Cooldown is already
enforced by `in_cooldown()` (message count since `last_compaction_msg_count`). Keep
writing `cooldown_turns_left` only for `status` display, but compute the displayed value in
`cmd_ac_status` as `max(0, cfg.cooldown_turns - turns_since)`.

**Acceptance:**
- New test `test_cooldown_expires`: `mkcfg(cooldown_turns=3)`; `note_success(...)`; append 3
  messages; `assess(s, cfg).recommendation != "skip"` and `"still inside cooldown"` not in blockers.
- `test_cooldown_blocks_reevaluation` still passes.

## Task 2 — Implement `rearm_below_percent` hysteresis

**Why:** after a successful compaction the controller should not re-evaluate until pressure
has dropped below `rearm_below_percent` (45) at least once, *or* the cooldown has passed.
Currently the config key is validated but unused.

**Change:** in `AutoCompactController`: add `meta["armed"]` (bool, default `True`).
`note_success` sets it `False`. In `should_evaluate` and at the top of `evaluate` (after the
disabled check): if `pressure < rearm_below_percent/100` set `armed=True`. If `armed` is
`False` and pressure `< force` → return `{"action": "cooldown"}`. Emergency always bypasses.
`reset_cooldown()` sets `armed=True`.

**Acceptance (tests):**
- `test_rearm_blocks_until_below_45`: after `note_success`, pressure 0.7 → action `cooldown`.
- `test_rearm_after_drop`: set window so pressure 0.3, call `evaluate` (returns `below_threshold`),
  then raise pressure to 0.7 with enough new turns → no longer `cooldown`.
- `test_emergency_ignores_rearm`: pressure 0.95 right after success → not `cooldown`.

## Task 3 — Honor "Later" (`L`) and the `default_action` config

**Why:** prompt option `L` stores `meta["postponed_until_msg_count"]` which nothing reads, and
`default_action` ("postpone|skip|apply") is unused.

**Change:** in `evaluate`, next to the existing `postpone` block: if
`meta["postponed_until_msg_count"]` is set and `len(messages) < it` and pressure `< force`
→ return `{"action": "postponed_turns"}`; otherwise delete the key. Apply `cfg.default_action`
when the UI returns an unparseable answer or EOF: `postpone` → same as `L`; `skip` → return
`skipped`; `apply` → proceed to build the candidate.

**Acceptance:**
- `test_later_default_postpones`: UI answers `"l"`; second `evaluate` immediately after returns
  `postponed_turns`.
- `test_later_expires`: after `cooldown_turns` more messages, `evaluate` proceeds.

## Task 4 — Make the live wrapper actually prompt and use a checkpoint (BUG)

**Why:** `wrapper._maybe_auto_compact` calls `controller.evaluate(...)` with no `ui=` and
`checkpoint=None`. In `ask` mode nothing is ever asked, and risk/boundary detection is blind.

**Change:** in `wrapper.py::_maybe_auto_compact`: import `capture_checkpoint` and `PromptUI`;
compute `ckpt = capture_checkpoint(s)`; pass `checkpoint=ckpt` and, when `interactive`,
`ui=PromptUI()`. Also make `apply_fn` available whenever `acfg.ask_user != "always"` or
`interactive` (currently it is `None` for non-interactive non-`apply` modes, so
`apply-if-valid` can never run). Because the wrapper owns the child's stdin/stdout, add a
`PromptUI(input_fn=..., output_fn=...)` that writes to `sys.stderr` and reads from the real
terminal (`sys.__stdin__`); do not read from the child pipe.

**Acceptance:** extend `tests/test_wrapper.py` with a fake child + fake `PromptUI`:
- `--auto-compact=ask` at 80% pressure → fake UI `ask` called ≥ 1.
- `--auto-compact=apply` non-TTY at 80% → action `applied` or `rejected` (never blocks).
- `--auto-compact=off` → controller never constructed.

## Task 5 — Populate checkpoint fields from evidence

**Why:** `capture_checkpoint` always returns `action_permission="unknown"` and empty
`files_modified_*`, so the `files_modified_by_user` risk rule can never fire.

**Change (all deterministic, no LLM), in `capture_checkpoint`:**
- `action_permission`: scan last 10 messages (lowercase). `"git push"` allowed → `push_allowed`;
  `"commit"` → `commit_allowed`; message role `tool` present → `commands_allowed`;
  `"edit"|"write file"|"apply patch"` → `edit_allowed`; else `"unknown"`.
- `files_modified_by_agent`: file paths in assistant messages matching `(edited|wrote|created|modified)\s+(\S+\.\w+)`.
- `files_modified_by_user`: file paths in user messages matching `(I (modified|changed|edited))\s+(\S+\.\w+)`.
- `pending_steps`: lines in the last assistant/user message starting with `- [ ]` or `TODO:`.
- `next_action`: text after `next:` / `next step:` (case-insens.) in the last 6 messages, else `"unknown"`.
- Never invent values: when no match, keep `unknown` / `[]`.

**Acceptance (tests):** one test per field with a 3-message session proving extraction, and
`test_checkpoint_unknowns_stay_unknown` for an empty-ish session.

## Task 6 — Checkpoint survives compaction and is verified

**Needs:** 5.

**Why:** the spec says the continuity block "must always survive". Today it is only printed.

**Change:**
1. In `evaluate`, before `apply_fn`, `ckpt = checkpoint or capture_checkpoint(self.s)`; store
   `ckpt_id = sha256(json.dumps(ckpt, sort_keys=True))[:12]` in `result["checkpoint_id"]`.
2. After building the candidate, if no message in it has tag `continuity_checkpoint`, append a
   `Message(role="user", ...)`-style system block: `Message(role="assistant"... )` is **not**
   allowed; use the same role as the existing summary message, tags `{"continuity_checkpoint"}`,
   `policy=KEEP_VERBATIM`, `content="## Continuity checkpoint\n" + json.dumps(ckpt, indent=2)`.
3. `verify(...)` gains an optional `checkpoint: dict | None = None` argument. Error
   `"continuity checkpoint missing"` if absent; error `"next_action not preserved"` if
   `ckpt["next_action"] not in ("unknown", "")` and not in candidate text; same for
   `action_permission`. Keep the argument optional so old call sites/tests work.
4. `repair()` re-appends the checkpoint message when it is missing.

**Acceptance:**
- `test_candidate_contains_checkpoint_after_apply`.
- `test_verify_fails_when_checkpoint_missing` and `test_repair_restores_checkpoint`.
- `drop_always` messages must never be added to the checkpoint (assert a secret absent).

## Task 7 — Missing verification checks

**Needs:** 6.

Add to `verify` (each adds an entry to `errors`, optional args so old calls still work):
- **Exact facts:** pass `exact_facts: list[str]`. Build it in `evaluate` from regex hits of
  `[\w./\\-]+\.(py|ts|js|json|yaml)`, backticked identifiers `` `x` `` and 7+ char hex ids in
  messages tagged `decision`/`constraint`/`goal` or `KEEP_VERBATIM`. Error
  `exact fact lost: <fact>` when a fact is missing from candidate text.
- **Unsupported claims:** for every item in the candidate's `summary_json["summary_high"]`
  keys `goals`, `constraints`, `decisions`: it must share ≥ 60% of its words with some original
  message (simple word-overlap). Otherwise error `unsupported <key>: <text[:60]>`.
- **Completed-task regression** must reference a *specific* completed item, not just the word
  "done" anywhere — keep the current check as fallback, add the specific one.

**Acceptance (tests):** `test_exact_fact_lost_fails` (retire the weak existing test only by
adding, not deleting), `test_unsupported_goal_fails`, `test_supported_goal_passes`.

## Task 8 — Audit gaps

**Needs:** 6.

In the audit dict built at apply time add `"continuity_checkpoint_id": result.get("checkpoint_id")`.
Also write a lightweight audit entry (`"applied": False`) to `s.meta["auto_compaction"]["history"]`
(list, cap 20) for outcomes `later`, `disabled`, `rejected`, `circuit_breaker` with
`user_action` ∈ `apply|added_essence|later|disabled|null`, `state_before`, `state_after`, `reason`.
`user_action` must be set from the real UI verdict (today it is hard-coded to `apply` unless
essence was added).

**Acceptance:**
- `test_audit_has_checkpoint_id`.
- `test_history_records_later_and_disabled`.
- `test_audit_contains_no_secret` (existing) still passes; add a variant that includes the
  secret in the essence text — audit must store only `essence_directive_ids`, never the content.

## Task 9 — Missing assessment inputs

Independent. In `assess` add (all to `reasons`/`blockers`, never raising risk silently):
- **Immediate retrigger:** if `projected` pressure (`projected + overhead + reserve_output + expected_tool`) / window ≥ `evaluate_at_percent` → blocker `"candidate would immediately retrigger"` and `recommendation="skip"`.
- **Protected budget:** if `pinned_ratio > 0.8` → blocker `"protected content exceeds budget"`.
- **Previous failures:** if `meta["failed_attempts"] ≥ 1` add reason; risk `+1`.
- **Starts with a summary:** already in risk; also lower `projected_reduction` estimate (summary
  can't shrink further) by treating the existing summary message as pinned.
- **Natural task boundary:** if the last message contains `done|passed|committed|merged` and no
  open pair → reason `"natural task boundary"`.
- Window 75–90%: `recommendation` is `ask`/`apply` (already), plus add reason
  `"above recommend threshold"` when pressure ≥ recommend.

**Acceptance (tests):** one per bullet, using `sized(...)` and hand-built sessions.

## Task 10 — Interactive `[R]` review (Summary / Items / Diff)

**Needs:** 6.

**Why:** `_prompt_user` on `R` just returns `"review"`; `compact --review diff` prints a placeholder.

**Change:** new pure functions in `autocontrol.py` (no I/O):
- `review_summary(plan, assessment) -> str` → `KEEP: n / SUMMARIZE: n / DROP: n / RISK: X`.
- `review_items(session, plan) -> list[str]` → lines like `KEEP  m103  constraint  <text[:50]>`,
  `SUM   m120-m151  investigation`, `DROP  pair:c18  successful ls output`.
- `review_diff(old_summary: dict | None, new_summary: dict | None) -> str` → `+`/`-` lines per
  list key (`open_tasks`, `decisions`, …) comparing the previous and candidate structured summaries.
Then loop in `_prompt_user`: `S`/`I`/`D` print via `ui.info(...)` (fall back to `ui.ask`
if `info` is missing) and re-ask; `B` returns to the main menu. `cmd_compact --review` calls the
same functions instead of its placeholder.

**Acceptance (tests):** snapshot-style asserts on each function's output for a fixed session;
`test_review_loop_back` with a scripted UI `["r","s","i","d","b","a"]` ends in `applied`.

## Task 11 — Rollback snapshot and atomic save

**Needs:** 8.

**Change:** before `evaluate` mutates `self.s` (right before `self.s.messages = candidate.messages`)
save `rollback = copy.deepcopy(self.s.messages)` into `self.s.meta["auto_compaction"]["rollback"]`
as JSON-serializable dicts (use the same serializer as `store`), keeping only the **last** snapshot.
Add `AutoCompactController.rollback()` that restores it and returns `True/False`; add CLI
`ctxpipe auto-compact rollback`. In `store.save`, write to `session.jsonl.tmp` then
`os.replace` it over the real file.

**Acceptance:**
- `test_rollback_restores_messages` (apply, rollback, messages equal the originals).
- `test_save_is_atomic`: monkeypatch `os.replace` to raise; the original file content is unchanged.
- If `cfg.rollback_on_failure` is `False`, no snapshot is stored.

## Task 12 — Force re-warn and noninteractive critical behavior

**Needs:** 4.

**Change:**
- After a postpone, when pressure first reaches `force_at_percent`, interactive sessions show a
  one-line warning via `ui.info`/stderr: `ctxpipe: context is N% full — compaction was postponed`.
  Do it once (`meta["force_warned"]=True`, cleared on success).
- Noninteractive: `noninteractive_action="postpone"` must still apply when pressure ≥ force
  and verification passes ("Default to postpone unless pressure is critical").

**Acceptance:** `test_force_warns_once`, `test_noninteractive_postpone_but_critical_applies`
(same session at 0.8 → `postponed_noninteractive`; at 0.95 → `applied`/`rejected`).

## Task 13 — Example config and docs workflows

Create `examples/configs/ctxpipe-auto-compaction.yaml` (the YAML block from README, with
comments) and add to `docs/RECIPES.md` three terminal workflows with real command output
copied from running the commands on `examples/sessions/original.jsonl`:
1. Low-risk auto application (`ask_user: never`).
2. Medium-risk essence prompt (answer `e`, then text, then `a`).
3. Emergency compaction (≥ 90%).
Also document: "65% is an evaluation threshold, not a guaranteed compaction",
noninteractive defaults, rollback, and a CLI reference table.

**Acceptance:** `python -c "import yaml;yaml.safe_load(open('examples/configs/ctxpipe-auto-compaction.yaml'))"`
succeeds and `load_config` on a project containing it validates.

## Task 14 — Regression run (last)

1. `python -m pytest -q` → all pass (expected ≈ 81 + new tests).
2. Run `python benchmarks/run_benchmark.py` and `python examples/make_demo.py` (these may need
   `ANTHROPIC_API_KEY`; if so, state that and skip). Compare against committed
   `benchmarks/results/benchmark.json` and `examples/sessions/*.jsonl`; list any differences.
3. Update README test count, `CHANGELOG.md` (v2.1.1), `docs/ROADMAP.md`.

**Final report must include:** files changed, tests added/total, benchmark diff, remaining limitations.

---

## Definition of done (whole feature)

- All tasks above pass their acceptance checks; full suite green.
- With `auto_compaction.enabled: true` and `ask_user: smart`, a session at ~80% with a clean
  boundary and low risk is compacted automatically; with pending steps or user-edited files it
  prompts; at ≥ 90% it compacts without prompting if verification passes; unsafe boundary
  → `pending_boundary`; after success it stays quiet until cooldown **and** re-arm.
- Nothing is replaced when verification fails, and `ctxpipe auto-compact rollback` undoes an apply.
- Existing manual commands, configs, adapters and audit entries behave exactly as before.

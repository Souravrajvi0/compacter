# Recipes

Copy-pasteable `compacter.yaml` patterns and commands for common situations.
Config file location: `<project>/compacter.yaml`. See `examples/configs/` for full files.

## 1. Drop all successful tool logs, keep errors verbatim

Problem: successful `ls`/`pytest` outputs bury the signal.

```yaml
scopes:
  - match: "tool_result"
    rule:
      summarize_detail: low
      truncate_result_to: 30
      drop_result_if: no_errors   # drops only when no error/traceback/exit_code != 0
```

```bash
compacter compact --preview --profile aggressive
```

Expected effect: ~90% of successful result tokens gone; every error result survives in `summary_medium` or verbatim.

## 2. Keep goals/constraints forever, compress everything else after 2 compactions

```yaml
scopes:
  - match: "tag:goal"
    rule: {keep_verbatim_until: 999}
  - match: "tag:constraint"
    rule: {keep_verbatim_until: 999}
  - match: "role:user"
    rule: {keep_verbatim_until: 2}
```

```bash
compacter auto-tag --apply   # suggests goal/constraint tags first
compacter compact --apply
```

Expected effect: `summary_high` always lists goals/constraints; older user messages become summaries.

## 3. Auto-tag safely — suggest tags but never pin

```yaml
auto_tagging:
  enabled: true
  min_confidence: 0.7
  set_keep_verbatim_for_tags: []   # default: tags only
```

```bash
compacter auto-tag          # dry-run: prints suggestions with reasons
compacter auto-tag --apply  # writes the tags
```

Expected effect: tags appear; policies unchanged, so nothing becomes non-degradable.

## 4. Code-heavy workflow: preserve code blocks and paths, compress discussion

```yaml
scopes:
  - match: "file:*.py"
    rule: {summarize_detail: high}
  - match: "tool_result"
    rule: {keep_result_sections: ["errors"], truncate_result_to: 400}
  - match: "tag:experiment"
    rule: {summarize_detail: low}
```

```bash
cp examples/configs/compacter-code-heavy.yaml ./compacter.yaml
compacter compact --apply --profile code-heavy
```

Expected effect: failing test output and file paths survive in `summary_high`; exploratory chatter folds into `summary_low` or is dropped at budget.

## 5. Long-lived projects: conservative window, chained state

```yaml
scopes:
  - match: "tool_result"
    rule: {summarize_detail: medium, drop_result_if: no_errors}
  - match: "role:user"
    rule: {keep_verbatim_until: 5}
```

```bash
compacter compact --apply --profile conservative   # run weekly / per session
```

Expected effect: each compaction merges the previous structured summary — goals/decisions accumulate in `summary_high` instead of drifting.

## Automatic compaction

Full config: `examples/configs/compacter-auto-compaction.yaml`. Everything below is
reproducible offline with `python examples/auto_compaction_demo.py` (deterministic stub LLM).

**65% is an evaluation threshold, not a guaranteed compaction event.** Crossing it only
makes the session *eligible*: compacter still skips when the projected reduction is poor, when
a tool pair or risky phase (migration/rebase/commit/deploy) is open, during cooldown, or when
the candidate would immediately retrigger. After a successful compaction it stays quiet until
pressure drops below `rearm_below_percent` (or reaches the recommend threshold).

### Workflows (real output from the demo)

```text

=== 1. Low-risk automatic application (ask_user: never, 80% full) ===
-> action=applied  pressure=80%  risk=low  recommendation=ask
-> tokens 778 -> 186; checkpoint kept; rollback available: True

=== 2. Essence prompt (ask_user: always; ask_user: smart asks the same when risk is medium or higher) ===
compacter: context is 80% full.

Compaction recommendation:
  Current context:     778 tokens
  Projected context:   64 tokens
  Projected reduction: 91%
  Semantic risk:       LOW
  Main reason: above recommend threshold (75%)

Guaranteed preserved:
  1 goals
  1 constraints
  1 decisions
  current phase and next action
  pinned messages and exact facts

Plan:
  Keep verbatim:  pinned + recent
  Summarize:      middle turns
  Drop:           disposable tool output

[A] Apply
[E] Add essence
[R] Review plan
[L] Later
[N] Disable for this session
> e
What must survive this compaction?
> keep the exact adapter names and the rejected regex parser
Essence recorded: "keep the exact adapter names and the rejected regex parser". [A] Apply / [L] Later
> a
-> action=applied  pressure=80%  risk=low  recommendation=ask
-> tokens 778 -> 203; checkpoint kept; rollback available: True

=== 3. Emergency compaction (97% full: never asks, applies only if verified) ===
-> action=applied  pressure=97%  risk=low  recommendation=emergency
-> tokens 778 -> 186; checkpoint kept; rollback available: True
```

1. **Low-risk automatic application** - `ask_user: never` (or `smart` with low risk): verified, then applied silently.
2. **Essence prompt** - answer `E`, say what must survive ("keep the exact adapter names..."), then `A`.
   The directive is verified against the candidate and expires only after a successful apply.
   You can also type `keep X and Y; apply` directly. `L`, `L 10`, `L 15000t` postpone; `N` disables for this session only;
   `R` opens Summary / Items / structured Diff views.
3. **Emergency** - at or above `force_at_percent` it never asks; it applies only if verification passes
   (pinned messages, drop_always, essence, tool pairs, exact facts, continuity checkpoint, minimum reduction).

### Noninteractive behavior and rollback

- No TTY (CI, pipes): never blocks. `noninteractive_action: postpone` (default), `apply-if-valid`, or `disable`.
  Emergency pressure always applies if valid.
- `compacter wrap --auto-compact` (use config), `=ask`, `=apply`, `=off`. `ask`/`apply` enable the feature for that run.
- A candidate that fails verification is repaired once, then rejected; the original session is untouched.
- Before every automatic apply a snapshot is kept: `compacter auto-compact rollback` restores the master
  transcript (the agent's own session file is never overwritten, so there is nothing to undo there).
- Master transcript writes are atomic (temp file + rename).

### CLI reference

| Command | What it does |
|---|---|
| `compacter auto-compact status` | state, pressure, thresholds, cooldown, armed, rollback availability |
| `compacter auto-compact enable` / `disable` | toggle `auto_compaction.enabled` in `compacter.yaml` |
| `compacter auto-compact assess` | print the compactability assessment as JSON |
| `compacter auto-compact reset-cooldown` | clear cooldown / postpone / failure counters |
| `compacter auto-compact rollback` | restore the transcript from before the last automatic compaction |
| `compacter compact --auto` | one automatic evaluation (prompts when a TTY is attached) |
| `compacter compact --apply-if-valid` | automatic, never asks, applies only when verified |
| `compacter compact --checkpoint` | print the continuity checkpoint JSON |
| `compacter compact --verify` | check transcript integrity and pending essences |
| `compacter compact --review summary\|items\|diff` | inspect the plan (`diff` calls the LLM for a candidate) |
| `compacter wrap --auto-compact[=ask\|apply\|off]` | live wrapper integration |

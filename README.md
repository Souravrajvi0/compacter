# compacter

**Context control for CLI coding agents. You decide what the agent remembers.**

> Status: v2.2.0. 170 tests. Adapters: Claude Code, Codex, opencode, Aider, generic JSON.
> Verified against Claude Code 2.1.296, Codex 0.144.6 and opencode 1.18.35 (`compacter doctor`).

## The problem

Agentic compaction summarizes everything indiscriminately: your original goal,
stale tool logs, and pasted secrets all get treated the same — and you only
find out what was lost when the agent forgets a constraint you set an hour ago.

## The solution

compacter is a thin control layer between you and the agent. It keeps a master
transcript in one common format, and the agent only ever sees a *view* of it,
built from your policies and tags. Compaction is preview-then-apply: you read
the summary before the agent ever sees it.

**Key features**

- Policy-aware compaction: `keep_verbatim`, `summarize_ok`, `drop_ok`, `drop_always`
- Rich per-message rules: `keep_verbatim_until`, `drop_ok_after`, `summarize_detail`, tool-result truncation/section-keeping/error-conditional dropping
- Scoped policies in `compacter.yaml` (`file:`, `tag:`, `role:`, `tool_result`) — most-specific match wins, strictest breaks ties
- Structured, schema-driven JSON summaries (goals/constraints/decisions/open_tasks/key_files) with layered `summary_high/medium/low` that degrades gracefully under token pressure
- `tool_use`/`tool_result` pairing — compaction never splits a pair across the summary boundary
- Chained compactions: every `--apply` is recorded in `meta.compactions` and the previous structured summary is merged by the LLM
- Real token counts (tiktoken) with graceful fallback
- Prompt profiles: `--profile conservative|aggressive|code-heavy`
- `compacter auto-tag`: conservative heuristic tag suggestions (writes only with `--apply`)
- Adapters for Claude Code, Codex, opencode, Aider, and plain JSON
- Tick-box message picker (`compact --select`, `--only 0,3-5`) and `/compacter` slash commands inside Claude Code and Codex
- `compacter carry`: pick messages from a chat and carry only those into a new chat (summarized or word for word)
- Context packs: save a compaction and start any future chat, in any project or agent, from it
- No API key needed: summaries come from the agent CLI you already use (`--llm auto`)
- `compacter doctor`: compares your installed agent versions with the ones this release was tested against
- Live wrapper: `compacter wrap` intercepts `/compact` inside the agent
- Automatic compaction (opt-in): pressure-based triggers, risk assessment, verification, circuit breakers — see below
- LLM-as-judge evals: `compacter eval --task ...` checks the compacted view still supports the task
- Benchmarks vs naive agent compaction — see docs/BENCHMARKS.md (~5.7× token cut, no secret leakage)

## Getting started

```bash
npm install -g compacter          # launcher; sets up its own private Python environment (needs Python 3.10+)
pip install compacter-agent       # from PyPI once published (installs the `compacter` command)
pip install .                     # or from a clone of this repo
pip install ".[dev,anthropic]"    # with tests + Anthropic summarizer
# No API key needed: summaries are written by the agent you already use (claude / codex / opencode).
# Optional: export ANTHROPIC_API_KEY=... and pass --llm anthropic to use the API instead.
```

The old command name `ctxpipe` still works as an alias. On Windows, if `compacter` is not found after `pip install`,
add Python's `Scripts` folder to PATH or run `python -m compacter`.

## Quickstart

Works with any adapter. Example with Aider:

```bash
cd ./my-repo
compacter start --agent aider          # import the current session
compacter status                       # see messages with tags/policies
compacter tag last --as goal           # pin the original task
compacter mark 4 --policy drop_always  # keep a pasted secret out of context
compacter compact --preview            # see the plan + summary
compacter compact --apply              # write the compacted session
```

## Tags and policies

| Tag | Effect |
|---|---|
| `goal` | Pinned: always kept verbatim |
| `constraint` | Pinned: always kept verbatim |
| `decision` | Informational; tag it to find it again |
| `compaction_summary` | Added automatically to the folded summary |

| Policy | Meaning |
|---|---|
| `keep_verbatim` | Never summarized, never dropped |
| `summarize_ok` | Default; may be folded into the summary |
| `drop_ok` | May be discarded when compacting |
| `drop_always` | Never enters the agent's context (secrets, noise) |

## Workflow examples

**Long coding session with tool logs**

```bash
compacter start --agent codex
for i in 3 5 7; do compacter mark $i --policy drop_ok; done
compacter drop-last 4            # last failed attempts shouldn't stick
compacter compact --preview
compacter compact --apply
```

**Keep constraints, drop experimental branches**

```bash
compacter tag 0 --as constraint
compacter tag 1 --as goal
compacter mark last --policy drop_always   # discard the branch you abandoned
compacter compact --apply --threshold 6000 # only compacts if over 6k tokens
```

**Repeated compactions over days**

Each `--apply` replaces the master transcript and appends to `meta.compactions`,
so today's summary becomes input to tomorrow's compaction instead of being lost.

```bash
compacter compact --apply --keep-recent 4
# ... work more ...
compacter compact --apply --keep-recent 4   # chained: previous summary is kept
```

**Live wrapper**

```bash
compacter wrap --agent claude-code -- claude
```

Inside the agent session, typing `/compact` does not run the agent's own
compaction. compacter intercepts it: it builds a policy-aware summary, writes a
new session file (the original is never overwritten), ends the agent process,
and prints the `--resume` command for the compacted session. Sessions live in
`<project>/.compacter/session.jsonl` (master) and each agent's native store.

## Advanced: scoped policies & rules

`compacter.yaml` in the project root overrides scopes only where set (copy-ready
files in `examples/configs/`, patterns in `docs/RECIPES.md`):

```yaml
scopes:
  - match: "file:*.log"
    rule: {summarize_detail: low, drop_result_if: no_errors}
  - match: "tag:experiment"
    rule: {summarize_detail: low}
  - match: "role:user"
    rule: {keep_verbatim_until: 3}

auto_tagging:
  enabled: true
  min_confidence: 0.7
  set_keep_verbatim_for_tags: ["goal", "constraint"]
```

Precedence: `file:` > `tag:` > `role:` > `tool_result` > default; ties broken by the
strictest rule; an explicit per-message `rule` always wins field-by-field.

`compacter auto-tag` scans for `goal:`/`constraint:`/`we decided` patterns and
suggests tags (tags only by default; set-keep-verbatim via the config list or
`--also-set-keep-verbatim`).

## Automatic compaction (opt-in)

Off by default. Enable with `compacter auto-compact enable` (writes `compacter.yaml`) or
set `auto_compaction.enabled: true`. compacter watches *effective* context pressure
(visible tokens + system/tool schemas + reserved output/tool tokens, relative to
`context_window`) and acts at three thresholds:

| Pressure | Behavior |
|---|---|
| `< evaluate_at_percent` (65) | nothing |
| `65–75` | compact only if projected reduction is worthwhile and risk is low |
| `>= recommend_at_percent` (75) | recommend; ask in `smart` mode when risk is not low |
| `>= force_at_percent` (90) | emergency: apply if verification passes, never asks |

Safety rails:

- **Safe boundaries only** — never mid `tool_use`/`tool_result` pair, or while the
  checkpoint phase is migrating/rebasing/committing/deploying.
- **Risk assessment** (`compacter auto-compact assess`) scores semantic risk from pinned
  ratio, pending steps, user-modified files and unresolved questions.
- **Verification before apply** — pinned messages preserved, `drop_always` content not
  leaked, essence directives satisfied, tool pairs intact, completed work not
  regressed to pending, minimum reduction met. One deterministic repair attempt.
- **Circuit breakers & hysteresis** — cooldown turns, minimum new tokens, max
  attempts, and "same source failed twice" stops retry loops.
- **Essence directives** — answer `E` at the prompt to say what must survive the
  next compaction; it is verified, then expires after a successful apply.
- **Continuity checkpoint** — phase, permissions, next action, files touched by you vs the
  agent, pending steps are captured deterministically and kept as one pinned block that is
  verified to survive every compaction.
- **Rollback** — a snapshot is taken before each automatic apply (`compacter auto-compact rollback`);
  master-transcript writes are atomic. Recipes and real output: `docs/RECIPES.md`.
- **Audit trail** — every automatic apply is recorded in `meta.compactions`
  (`automatic: true`, pressure, assessment, verification, user action).

```yaml
auto_compaction:
  enabled: true
  context_window: 200000
  evaluate_at_percent: 65
  recommend_at_percent: 75
  force_at_percent: 90
  rearm_below_percent: 45
  keep_recent: 6
  ask_user: smart            # always | smart | never | critical-only
  noninteractive_action: postpone   # postpone | apply-if-valid | disable
  min_projected_reduction: 0.30
  cooldown_turns: 12
```

```bash
compacter auto-compact status|enable|disable|assess|reset-cooldown|rollback
compacter compact --auto                 # one automatic evaluation (prompts if needed)
compacter compact --apply-if-valid       # automatic, never asks, applies only if verified
compacter wrap --agent claude-code --auto-compact -- claude   # bare flag = use config; or ask|apply|off
```

## Carry chosen messages into a new chat

You don't have to compact a whole chat. Pick just the messages worth keeping (the same ✓ checklist,
or index ranges), and bring only those into a fresh chat. The original chat is never changed.

```bash
compacter carry --agent opencode                  # checklist: tick messages, they are summarized
compacter carry --agent codex --only 3-9,14 --new # ranges; also writes a ready-to-resume new chat
compacter carry --agent claude-code --only 0 --verbatim --name vault   # carry word for word
```

In Claude Code or Codex (with the hook installed):

```
/compacter carry                       list every message
/compacter carry 3-9,14 as auth-fix    summarize those into a pack named auth-fix
/compacter carry 3-9 verbatim new      carry word for word and write a new chat
/compacter pack load auth-fix          then, in the new chat, bring the pack in
```

## Context packs: reuse a compaction in a new chat

A compaction is worth keeping. Save it as a **pack** and start any future chat from it, in any
project and with any agent. Packs hold the latest summary plus the messages you pinned
(goal / constraint / keep_verbatim) and live in `~/.compacter/packs` (override with `COMPACTER_HOME`).

```bash
compacter compact --apply                      # compact a chat as usual
compacter pack save login-fix --note "auth work"
compacter pack list
compacter new --agent claude-code --from login-fix   # a brand-new chat that starts with the pack
```

Inside Claude Code (with `compacter install-claude-code`), no terminal switch is needed:

```
/compacter pack save login-fix     save the latest compaction
/compacter pack load login-fix     add the pack to THIS chat (use it first thing in a new chat)
/compacter pack new login-fix      or write a fresh chat that starts with it
```

A seeded chat keeps the structured summary, so compacting it later merges the pack into the
next summary instead of losing it. Resume with `claude --resume <id>`, `codex resume --last`,
or `opencode --session <id>`.

## Agent compatibility

compacter reads each agent's own session files, which can change between releases. Verified versions
(each has a fixture in `tests/data/compat/`): Claude Code 2.1.296, Codex 0.144.6, opencode 1.18.35.
Run `compacter doctor` to compare your installed versions with these.

## Why this exists

CLI coding agents fill their context window and then compact it themselves —
lossy, opaque, and unsteerable. A secret pasted once keeps being re-sent
forever; a failed experiment keeps eating tokens after it stopped mattering.
compacter treats context as something *you* manage: tags and policies decide
what is summarized, deterministically; the LLM only decides how to phrase it.
This is the same "keep-verbatim zones, selective eviction" pattern used in
production context-engineering, made explicit and portable across agents.

## Architecture

```
            user
             |
          compacter CLI
             |
   +---------+----------+
   |   core (no agent   |   models: Message / Policy / Session
   |   knowledge)       |   filtering, tagging, compaction engine
   +---------+----------+
             |
        Adapter interface  <-- one module per agent
   +---------+---------+----------+
   | claude-code | aider | codex | opencode | generic-api
   +-------------+-------+-------+
             |
       agent's native session format
```

Principles:

- **The master transcript is the source of truth.** Compacted views are
  derived from it; adapters never overwrite the agent's own session file.
- **The core never imports agent specifics.** Adding an agent = one
  `Adapter` subclass with `@register`.
- **Deterministic where it can be.** Policies and tags decide *what*;
  the LLM decides *how* to phrase.

## Repo layout

```
src/compacter/core/       models, store, compaction, scopes, summary, autocontrol, autotag, packs, tokens, prompts
src/compacter/adapters/   base + registry, claude_code, codex, opencode, aider, generic_api
src/compacter/llm/        summarizers: agent CLIs (cli_llm), Anthropic API, key pool
src/compacter/integrations/  opencode TUI plugin
src/compacter/cli.py      argparse front end
src/compacter/hook.py     /compacter slash-command handler for Claude Code and Codex
src/compacter/tui.py      tick-box message picker
src/compacter/carry.py    carry chosen messages into a new chat
src/compacter/compat.py   verified agent versions + `compacter doctor`
src/compacter/wrapper.py  live wrapper mode
src/compacter/evals.py    LLM-as-judge evals
npm/                   npm launcher package (`compacter`)
.github/workflows/     CI (Linux/Windows/macOS) and release
examples/              demo repo: before/after compaction snapshots
evals/                 canonical eval tasks + results
benchmarks/            naive-vs-compacter compaction benchmark
tests/                 engine, store, adapters, hook, wrapper, packs, carry, evals, per-version compat
tests/data/compat/     one fixture per verified agent version
docs/ROADMAP.md        done/next
docs/BENCHMARKS.md     benchmark write-up
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

To add an adapter, see CONTRIBUTING.md.

## Status

- v2.2.0: context packs, `carry`/`new`, agent-CLI summarizer (no API key), per-version compatibility tests, `doctor`
- v2.1.x: opt-in automatic compaction with verification, checkpoints and rollback
- v2.0: rules engine, scoped policies, structured layered summaries, auto-tag, profiles, eval metrics
- Adapters: Claude Code, Codex, opencode, Aider, generic JSON
- 170 tests passing
- Verified end to end on real Claude Code: compact a 418-message chat (about 59k to about 2k tokens) and resume it
- Not yet verified in real use: the Codex hook, the opencode plugin, and editing code from a compacted chat

## License

MIT

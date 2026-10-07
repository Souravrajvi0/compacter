"""Prompts sent to the compaction LLM."""

COMPACTION_SYSTEM = """\
You compress the history of a coding-agent conversation so the agent can keep working \
with a smaller context. You will receive messages to summarize, each with an id, role, \
tags, policy and a detail hint (high = keep more, low = compress hard).

Output ONLY a JSON object with exactly this shape, no markdown fences, no prose:

{schema_example}

Rules:
1. summary_high: distilled, high-signal state. goals/constraints/decisions from \
<pinned> messages MUST appear here verbatim (they are the agent's standing orders).
   open_tasks and key_files reflect the CURRENT state, not the whole history.
2. summary_medium: the reasoning that matters — why approaches were rejected, what \
was tried.
3. summary_low: compressed exploratory discussion; keep it to a few sentences.
4. NEVER include content from messages whose content is gone or that are marked \
drop_always — treat them as if they never existed.
5. Tool results: for results with no errors, compress to 1–2 sentences (or omit) \
when policy allows. For errors, failing tests, or critical outputs, keep them at \
high detail in summary_medium or summary_high.
6. Do NOT invent facts. If unsure, omit. Target at most {target_tokens} tokens total.
"""

COMPACTION_USER = """\
<pinned note="kept verbatim elsewhere; context only">
{pinned}
</pinned>

<to_summarize>
{to_summarize}
</to_summarize>
"""

LEGACY_COMPACTION_SYSTEM = """\
You compress the history of a coding-agent conversation so the agent can keep working \
with a smaller context. Write ONE plain-markdown summary with these sections: \
## Progress, ## Decisions, ## Files & Commands, ## Open Issues. Target at most \
{target_tokens} tokens. No preamble.
"""

SUMMARY_HEADER = "[ctxpipe compaction summary of earlier conversation]\n"


def _schema_example() -> str:
    from .summary import SCHEMA_EXAMPLE

    return SCHEMA_EXAMPLE


def system_prompt(profile: str = "balanced", target_tokens: int = 1500) -> str:
    base = COMPACTION_SYSTEM.replace("{schema_example}", "\x00SCHEMA\x00")
    base = base.format(target_tokens=target_tokens)
    base = base.replace("\x00SCHEMA\x00", _schema_example())
    if profile == "conservative":
        base += "\nBe conservative: prefer keeping detail over compression. When in doubt, keep."
    elif profile == "aggressive":
        base += "\nBe aggressive: compress hard. The view must stay well under the token target."
    elif profile == "code-heavy":
        base += ("\nThis session is code-heavy: prioritize key_files, exact commands, file paths, "
                 "and failing test names. Code snippets matter more than prose.")
    elif profile != "balanced":
        raise ValueError(f"unknown profile: {profile}")
    return base


PROFILES = ("balanced", "conservative", "aggressive", "code-heavy")

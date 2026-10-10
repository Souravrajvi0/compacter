import json
from pathlib import Path

from compacter.config import ScopeRule
from compacter.core import compaction, store
from compacter.core.autotag import apply, suggest
from compacter.core.models import Message, Policy, Rule, Session
from compacter.core.summary import format_summary_block, parse_structured_summary


def msg(role="user", content="x", **kw):
    return Message(role, content, **kw)


def test_scope_specificity_ordering():
    scopes = [
        ScopeRule("role:user", Rule(summarize_detail="low")),
        ScopeRule("tag:experiment", Rule(summarize_detail="low")),
        ScopeRule("file:*.log", Rule()),
    ]
    m = msg("user", "hi", tags={"experiment"})
    m.meta["path"] = "run.log"
    r = compaction.effective_rule(m, scopes)
    # file: is most specific and wins, its rule is the base
    assert r.summarize_detail is None  # file scope had no detail; no overlay from role/tag


def test_scope_tie_breaker_strictest_wins():
    scopes = [
        ScopeRule("tag:a", Rule(summarize_detail="low", truncate_result_to=100)),
        ScopeRule("tag:b", Rule(summarize_detail="high", truncate_result_to=500)),
    ]
    m = msg(tags={"a", "b"})
    r = compaction.effective_rule(m, scopes)
    assert r.summarize_detail == "high" and r.truncate_result_to == 500


def test_message_rule_overrides_scope():
    scopes = [ScopeRule("role:user", Rule(summarize_detail="low"))]
    m = msg("user", "hi", rule=Rule(summarize_detail="high"))
    assert compaction.effective_rule(m, scopes).summarize_detail == "high"


def test_keep_verbatim_until_expires():
    s = Session(agent="g", project="p")
    s.messages = [msg("assistant", "old", rule=Rule(keep_verbatim_until=2)), msg("user", "new")]
    s.meta["compactions"] = [{"at": "t1"}]
    p1 = compaction.plan(s, keep_recent=0)
    assert p1.pinned[0].content == "old"  # 1 < 2, still pinned
    s.meta["compactions"] = [{"at": "t1"}, {"at": "t2"}]
    p2 = compaction.plan(s, keep_recent=0)
    assert p2.pinned == []  # expired → summarizable now
    assert p2.to_summarize[0].content == "old"


def test_drop_ok_after_gates_dropping():
    s = Session(agent="g", project="p")
    fresh = msg("user", "fresh", policy=Policy.DROP_OK, rule=Rule(drop_ok_after=100))
    old = msg("user", "old", policy=Policy.DROP_OK)
    s.messages = [old, msg("assistant", "pad"), fresh]
    p = compaction.plan(s, keep_recent=0)
    dropped_contents = [m.content for m in p.dropped]
    assert "old" in dropped_contents and "fresh" not in dropped_contents


def test_no_errors_drop_and_keep():
    s = Session(agent="g", project="p")
    ok_result = msg("tool", "all good, 3 files written", rule=Rule(drop_result_if="no_errors"))
    err_result = msg("tool", "Traceback: boom", rule=Rule(drop_result_if="no_errors"))
    s.messages = [msg("user", "run"), ok_result, err_result]
    p = compaction.plan(s, keep_recent=0)
    assert [m.content for m in p.dropped] == ["all good, 3 files written"]
    assert "Traceback: boom" in [m.content for m in p.to_summarize]


def test_exit_code_marks_error():
    m = msg("tool", "exit 0 output")
    m.meta["exit_code"] = 1
    assert compaction.result_has_errors(m)


def test_truncate_result_to_in_view():
    content = "word " * 500
    s = Session(agent="g", project="p")
    s.messages = [msg("tool", content, rule=Rule(truncate_result_to=20))]
    p = compaction.plan(s, keep_recent=1)
    view = compaction.rebuild(s, p, "")
    assert compaction.estimate_tokens(view.messages[0].content) <= 40  # head+tail+marker slack


def test_keep_result_sections():
    s = Session(agent="g", project="p")
    content = "## noise\nblah\n## Errors\nboom\n## summary\nok"
    s.messages = [msg("tool", content, rule=Rule(keep_result_sections=["errors"]))]
    p = compaction.plan(s, keep_recent=1)
    view = compaction.rebuild(s, p, "")
    assert "boom" in view.messages[0].content and "blah" not in view.messages[0].content


def test_structured_summary_parse_and_validate():
    good = json.dumps({
        "summary_high": {"goals": ["g"], "constraints": [], "decisions": [{"what": "w", "why": "y", "alternatives_rejected": []}], "open_tasks": [], "key_files": []},
        "summary_medium": "m", "summary_low": "l",
    })
    assert parse_structured_summary(good) is not None
    assert parse_structured_summary("```json\n" + good + "\n```") is not None
    assert parse_structured_summary("not json") is None
    assert parse_structured_summary('{"summary_high": {"goals": "nope"}}') is None


def test_layered_budget_degradation():
    data = parse_structured_summary(json.dumps({
        "summary_high": {"goals": ["g"], "constraints": [], "decisions": [], "open_tasks": [], "key_files": []},
        "summary_medium": "reason " * 200, "summary_low": "verbose " * 400,
    }))
    small = format_summary_block(data, token_budget=30)
    assert "Reasoning" not in small and "Exploratory" not in small
    big = format_summary_block(data, token_budget=10_000)
    assert "Reasoning" in big and "Exploratory" in big


def test_summarize_retries_then_falls_back():
    class FlakyLLM:
        def __init__(self):
            self.n = 0

        def complete(self, system, user, max_tokens=2048):
            self.n += 1
            return "definitely not json"

    s = Session(agent="g", project="p")
    s.messages = [msg("user", "hello")]
    p = compaction.plan(s, keep_recent=0)
    out = compaction.summarize(p, FlakyLLM())
    assert out == "definitely not json"  # legacy fallback, no crash


def test_render_includes_detail_hint():
    scopes = [ScopeRule("role:user", Rule(summarize_detail="low"))]
    s = Session(agent="g", project="p")
    s.messages = [msg("user", "hi")]
    p = compaction.plan(s, keep_recent=0)
    assert 'detail="low"' in compaction._render(p.to_summarize, scopes)


def test_autotag_suggests_and_apply_sets_tags_only(tmp_path: Path):
    s = Session(agent="g", project="p")
    s.messages = [msg("user", "Goal: ship the tool"), msg("user", "Constraint: no secrets in logs")]
    sugs = suggest(s)
    assert {(x.message.content[:4], x.tag) for x in sugs} == {("Goal", "goal"), ("Cons", "constraint")}
    n = apply(s, sugs)  # default: tags only, no policy change
    assert n == 2 and all(m.policy is Policy.SUMMARIZE_OK for m in s.messages)
    s2 = Session(agent="g", project="p")
    s2.messages = [msg("user", "Goal: x")]
    n2 = apply(s2, suggest(s2), keep_verbatim_for={"goal"})
    assert s2.messages[0].policy is Policy.KEEP_VERBATIM


def test_profiles_have_distinct_prompts():
    from compacter.core.prompts import system_prompt

    a = system_prompt("conservative", 1500)
    b = system_prompt("aggressive", 1500)
    c = system_prompt("code-heavy", 1500)
    assert a != b != c and "conservative" in a and "aggressive" in b and "code-heavy" in c


def test_structured_summary_flows_through_rebuild_and_chains(tmp_path: Path, monkeypatch):
    good = json.dumps({
        "summary_high": {"goals": ["ship"], "constraints": [], "decisions": [], "open_tasks": ["t1"], "key_files": []},
        "summary_medium": "we tried x", "summary_low": "",
    })

    class GoodLLM:
        def complete(self, system, user, max_tokens=2048):
            return good

    s = Session(agent="g", project="p")
    s.messages = [msg("user", "hello"), msg("assistant", "world")]
    p = compaction.plan(s, keep_recent=0)
    summary = compaction.summarize(p, GoodLLM())
    view = compaction.rebuild(s, p, summary)
    sm = [m for m in view.messages if "compaction_summary" in m.tags][0]
    assert "Goals" in sm.content and "summary_json" in sm.meta
    # chained run: previous summary JSON is handed back into the prompt
    seen = {}

    class CapLLM:
        def complete(self, system, user, max_tokens=2048):
            seen["user"] = user
            return good

    p2 = compaction.plan(s, keep_recent=0)
    compaction.summarize(p2, CapLLM(), previous_summary=json.loads(sm.meta["summary_json"]))
    assert "Previous structured summary" in seen["user"]

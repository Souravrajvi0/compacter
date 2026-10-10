"""Tests for docs/AUTO_COMPACTION_TASKS.md tasks 1-12."""
import io
import json
import os
from pathlib import Path

import pytest

from compacter import wrapper
from compacter.core import compaction, store
from compacter.core.autocontrol import (
    CHECKPOINT_TAG, AutoCompactController, assess, capture_checkpoint, collect_exact_facts,
    insert_checkpoint, repair, review_diff, review_items, review_summary, transcript_hash, verify,
)
from compacter.core.models import Message, Policy, Session
from compacter.core.tokens import session_tokens

from test_auto_compaction import FakeLLM, big_session, candidate_from, mkcfg, msg, sized

SAFE = {"phase": "implementation", "pending_steps": [], "files_modified_by_user": [],
        "next_action": "unknown", "unresolved_questions": []}


class ScriptUI:
    """Scripted prompt answers; records everything shown via info()."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []
        self.infos = []

    def ask(self, text):
        self.asked.append(text)
        return self.answers.pop(0) if self.answers else ""

    def info(self, text):
        self.infos.append(text)


def run(s, cfg, **kw):
    kw.setdefault("interactive", False)
    kw.setdefault("apply_fn", lambda x: candidate_from(x))
    kw.setdefault("checkpoint", SAFE)
    return AutoCompactController(s, cfg).evaluate(**kw)


# ------------------------------------------------------------------ task 1
def test_cooldown_expires():
    cfg = mkcfg(cooldown_turns=3)
    s = big_session(6, chunk=200)
    ctrl = AutoCompactController(s, cfg)
    ctrl.note_success(transcript_hash(s), len(s.messages))
    for i in range(3):
        s.messages.append(msg(f"more {i} " + "x" * 200, role="assistant"))
    cfg = sized(s, cfg, 0.8)
    a = assess(s, cfg)
    assert "cooldown" not in " ".join(a.blockers)
    assert a.recommendation != "skip"
    assert ctrl.cooldown_turns_left() == 0


# ------------------------------------------------------------------ task 2
def test_rearm_blocks_until_below_45():
    cfg = mkcfg()
    s = big_session(10, chunk=200)
    cfg = sized(s, cfg, 0.7)
    ctrl = AutoCompactController(s, cfg)
    ctrl.note_success(transcript_hash(s), len(s.messages))
    r = ctrl.evaluate(interactive=False, apply_fn=lambda x: candidate_from(x))
    assert r["action"] == "cooldown"


def test_rearm_after_drop():
    cfg = mkcfg()
    s = big_session(10, chunk=200)
    cfg = sized(s, cfg, 0.7)
    ctrl = AutoCompactController(s, cfg)
    ctrl.note_success(transcript_hash(s), len(s.messages))
    window = cfg.context_window
    cfg.context_window = window * 3  # pressure ~23%: below rearm
    assert ctrl.evaluate(interactive=False)["action"] == "below_threshold"
    assert ctrl._meta["armed"] is True
    cfg.context_window = window
    assert ctrl.evaluate(interactive=False, checkpoint=SAFE,
                         apply_fn=lambda x: candidate_from(x))["action"] != "cooldown"


def test_emergency_ignores_rearm():
    cfg = mkcfg()
    s = big_session(10, chunk=200)
    cfg = sized(s, cfg, 0.95)
    ctrl = AutoCompactController(s, cfg)
    ctrl.note_success(transcript_hash(s), len(s.messages))
    assert ctrl.evaluate(interactive=False, checkpoint=SAFE,
                         apply_fn=lambda x: candidate_from(x))["action"] != "cooldown"


# ------------------------------------------------------------------ task 3
def _later_setup(**kw):
    cfg = mkcfg(ask_user="always", cooldown_turns=12, **kw)
    s = big_session(10, chunk=200)
    return s, sized(s, cfg, 0.8)


def test_later_default_postpones():
    s, cfg = _later_setup()
    ctrl = AutoCompactController(s, cfg)
    r1 = ctrl.evaluate(ui=ScriptUI(["l"]), interactive=True, checkpoint=SAFE, apply_fn=lambda x: candidate_from(x))
    assert r1["action"] == "postponed"
    r2 = ctrl.evaluate(ui=ScriptUI(["l"]), interactive=True, checkpoint=SAFE, apply_fn=lambda x: candidate_from(x))
    assert r2["action"] == "postponed_turns"


def test_later_expires():
    s, cfg = _later_setup()
    ctrl = AutoCompactController(s, cfg)
    ctrl.evaluate(ui=ScriptUI(["l"]), interactive=True, checkpoint=SAFE, apply_fn=lambda x: candidate_from(x))
    for i in range(12):
        s.messages.append(msg(f"later {i} " + "x" * 200, role="assistant"))
    sized(s, cfg, 0.8)
    r = ctrl.evaluate(ui=ScriptUI(["l"]), interactive=True, checkpoint=SAFE, apply_fn=lambda x: candidate_from(x))
    assert r["action"] != "postponed_turns"


def test_default_action_skip_and_apply():
    s, cfg = _later_setup(default_action="skip")
    r = AutoCompactController(s, cfg).evaluate(ui=ScriptUI([""]), interactive=True, checkpoint=SAFE,
                                               apply_fn=lambda x: candidate_from(x))
    assert r["action"] == "skipped"
    s, cfg = _later_setup(default_action="apply")
    r = AutoCompactController(s, cfg).evaluate(ui=ScriptUI([""]), interactive=True, checkpoint=SAFE,
                                               apply_fn=lambda x: candidate_from(x))
    assert r["action"] in ("applied", "rejected")


# ------------------------------------------------------------------ task 4 (wrapper)
class FakeChild:
    def __init__(self):
        self.stdin = io.StringIO()

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


def _wrapper_project(tmp_path, ratio=0.8, extra_cfg=""):
    import yaml

    s = big_session(10, chunk=200)
    s.project = str(tmp_path)
    s.messages.append(msg("hello"))  # the user line the wrapper will append has similar size
    store.save(s, tmp_path)
    window = int(session_tokens(s.visible()) / ratio)
    cfg = {"auto_compaction": {"enabled": True, "context_window": window, "reserve_output_tokens": 0,
                               "reserve_tool_tokens": 0, "min_turns_since_compaction": 0, "min_new_tokens": 0,
                               "cooldown_turns": 0, "min_projected_reduction": 0.2, "min_reduction": 0.2,
                               "keep_recent": 2}}
    cfg["auto_compaction"].update(yaml.safe_load(extra_cfg) or {})
    (tmp_path / "compacter.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def test_wrapper_ask_mode_prompts(tmp_path, monkeypatch):
    _wrapper_project(tmp_path)
    monkeypatch.setattr("compacter.llm.get_llm", lambda name="auto", **kw: FakeLLM())
    ui = ScriptUI(["l"])
    rc = wrapper._maybe_auto_compact(tmp_path, "generic-api", "x", 2, "ask", "hello\n", FakeChild(),
                                     ui=ui, interactive=True)
    assert rc is None
    assert len(ui.asked) >= 1


def test_wrapper_apply_mode_never_blocks(tmp_path, monkeypatch):
    _wrapper_project(tmp_path)
    monkeypatch.setattr("compacter.llm.get_llm", lambda name="auto", **kw: FakeLLM())
    rc = wrapper._maybe_auto_compact(tmp_path, "generic-api", "x", 2, "apply", "hello\n", FakeChild(),
                                     interactive=False)
    assert rc == 0  # compacted, child stopped
    assert (tmp_path / "messages.compacted.json").exists()


def test_wrapper_apply_if_valid_without_tty(tmp_path, monkeypatch):
    _wrapper_project(tmp_path, extra_cfg="{ask_user: always, noninteractive_action: apply-if-valid}")
    monkeypatch.setattr("compacter.llm.get_llm", lambda name="auto", **kw: FakeLLM())
    # ask_user=always + non-interactive + apply-if-valid used to have no compactor at all
    wrapper._maybe_auto_compact(tmp_path, "generic-api", "x", 2, "config", "hello\n", FakeChild(),
                                interactive=False)
    hist = store.load(tmp_path).meta["auto_compaction"]["history"]
    assert hist[-1]["outcome"] in ("applied", "rejected")  # a compactor was available and verification ran


def test_wrapper_off_never_builds_controller(tmp_path, monkeypatch):
    _wrapper_project(tmp_path)

    def boom(*a, **k):
        raise AssertionError("controller built")

    monkeypatch.setattr("compacter.core.autocontrol.AutoCompactController", boom)
    assert wrapper._maybe_auto_compact(tmp_path, "generic-api", "x", 2, "off", "hello\n", FakeChild()) is None


# ------------------------------------------------------------------ task 5
def test_checkpoint_extracts_fields():
    s = Session(agent="g", project="p")
    s.messages = [
        msg("I modified test_parser.py by hand", role="user"),
        msg("I edited src/parser.py and wrote notes.md. Next: run the tests", role="assistant"),
        Message("tool", "ok"),
        msg("- [ ] fix regex\nTODO: add docs", role="assistant"),
    ]
    c = capture_checkpoint(s)
    assert c["files_modified_by_user"] == ["test_parser.py"]
    assert c["files_modified_by_agent"] == ["notes.md", "src/parser.py"]
    assert c["next_action"] == "run the tests"
    assert c["pending_steps"] == ["fix regex", "add docs"]
    assert c["action_permission"] == "commands_allowed"


def test_checkpoint_permission_push_and_commit():
    s = Session(agent="g", project="p")
    s.messages = [msg("ok to commit")]
    assert capture_checkpoint(s)["action_permission"] == "commit_allowed"
    s.messages = [msg("go ahead and git push")]
    assert capture_checkpoint(s)["action_permission"] == "push_allowed"


def test_checkpoint_unknowns_stay_unknown():
    s = Session(agent="g", project="p")
    s.messages = [msg("hello")]
    c = capture_checkpoint(s)
    assert c["action_permission"] == "unknown" and c["next_action"] == "unknown"
    assert c["pending_steps"] == [] and c["files_modified_by_user"] == []


def test_checkpoint_excludes_drop_always():
    s = Session(agent="g", project="p")
    s.messages = [msg("Next: use sk-live-SECRET", policy=Policy.DROP_ALWAYS), msg("hi")]
    assert "SECRET" not in json.dumps(capture_checkpoint(s))


# ------------------------------------------------------------------ task 6
def test_candidate_contains_checkpoint_after_apply():
    cfg = mkcfg(ask_user="never")
    s = big_session(6, chunk=200)
    cfg = sized(s, cfg, 0.8)
    r = run(s, cfg)
    assert r["action"] == "applied", r
    cps = [m for m in s.messages if CHECKPOINT_TAG in m.tags]
    assert len(cps) == 1 and cps[0].policy is Policy.KEEP_VERBATIM


def test_verify_fails_when_checkpoint_missing_and_repair_restores():
    s = big_session(4, chunk=100)
    ckpt = {**capture_checkpoint(s), "next_action": "run the parser tests"}
    cand = candidate_from(s)
    v = verify(s, cand, [], mkcfg(), 0.5, checkpoint=ckpt)
    assert not v.passed and any("continuity checkpoint missing" in e for e in v.errors)
    fixed = repair(cand, s, [], checkpoint=ckpt)
    assert verify(s, fixed, [], mkcfg(), 0.5, checkpoint=ckpt).passed


def test_checkpoint_replaced_not_accumulated():
    s = big_session(2, chunk=20)
    first = insert_checkpoint(list(s.messages), {"phase": "testing"})
    second = insert_checkpoint(first, {"phase": "review"})
    cps = [m for m in second if CHECKPOINT_TAG in m.tags]
    assert len(cps) == 1 and "review" in cps[0].content


def test_second_compaction_keeps_single_checkpoint():
    cfg = mkcfg(ask_user="never")
    s = big_session(6, chunk=200)
    cfg = sized(s, cfg, 0.8)
    assert run(s, cfg)["action"] == "applied"
    for i in range(8):
        s.messages.append(msg(f"again {i} " + "x" * 200, role="assistant"))
    cfg = sized(s, cfg, 0.8)
    ctrl = AutoCompactController(s, cfg)
    ctrl.reset_cooldown()
    r = ctrl.evaluate(interactive=False, checkpoint=SAFE, apply_fn=lambda x: candidate_from(x))
    assert r["action"] in ("applied", "rejected", "skipped"), r
    assert sum(1 for m in s.messages if CHECKPOINT_TAG in m.tags) == 1


# ------------------------------------------------------------------ task 7
def test_exact_fact_lost_fails():
    s = big_session(4, chunk=100)
    s.messages.insert(2, msg("decision: use `customer_external_id` in parser.py", tags={"decision"}))
    facts = collect_exact_facts(s)
    assert "parser.py" in facts and "`customer_external_id`" in facts
    v = verify(s, candidate_from(s), [], mkcfg(), 0.5, exact_facts=facts)
    assert not v.passed and any("exact fact lost" in e for e in v.errors)
    ok = verify(s, candidate_from(s, essence_text="parser.py `customer_external_id`"), [], mkcfg(), 0.5,
                exact_facts=facts)
    assert not any("exact fact" in e for e in ok.errors)


class _GoalLLM:
    def __init__(self, goal):
        self.goal = goal

    def complete(self, system, user, max_tokens=2048):
        return json.dumps({"summary_high": {"goals": [self.goal], "constraints": [], "decisions": [],
                                            "open_tasks": [], "key_files": []},
                           "summary_medium": "", "summary_low": ""})


def _cand_with_goal(s, goal):
    p = compaction.plan(s, keep_recent=2)
    return compaction.rebuild(s, p, compaction.summarize(p, _GoalLLM(goal)))


def test_unsupported_goal_fails_and_supported_passes():
    s = big_session(4, chunk=100)
    bad = verify(s, _cand_with_goal(s, "Migrate everything to a quantum blockchain ledger"), [], mkcfg(), 0.5)
    assert any("unsupported goal" in e for e in bad.errors)
    good = verify(s, _cand_with_goal(s, "Goal: ship the parser"), [], mkcfg(), 0.5)
    assert not any("unsupported" in e for e in good.errors)


def test_specific_completed_item_regression():
    s = big_session(2, chunk=20)
    s.messages.insert(2, msg("unit tests passed for the parser module", role="assistant"))
    cand = candidate_from(s)
    cand.messages.append(msg("pending: unit tests passed for the parser module"))
    v = verify(s, cand, [], mkcfg(), 0.5)
    assert any("completed-task regression" in e for e in v.errors)


# ------------------------------------------------------------------ task 8
def test_audit_has_checkpoint_id_and_user_action():
    cfg = mkcfg(ask_user="never")
    s = big_session(6, chunk=200)
    cfg = sized(s, cfg, 0.8)
    r = run(s, cfg)
    e = s.meta["compactions"][-1]
    assert e["continuity_checkpoint_id"] == r["checkpoint_id"]
    assert e["user_action"] is None  # nobody was asked


def test_history_records_later_and_disabled():
    s, cfg = _later_setup()
    ctrl = AutoCompactController(s, cfg)
    ctrl.evaluate(ui=ScriptUI(["l"]), interactive=True, checkpoint=SAFE, apply_fn=lambda x: candidate_from(x))
    assert ctrl._meta["history"][-1]["outcome"] == "later"
    s2, cfg2 = _later_setup()
    ctrl2 = AutoCompactController(s2, cfg2)
    ctrl2.evaluate(ui=ScriptUI(["n"]), interactive=True, checkpoint=SAFE, apply_fn=lambda x: candidate_from(x))
    h = ctrl2._meta["history"][-1]
    assert h["outcome"] == "disabled" and h["user_action"] == "disabled"


def test_audit_never_stores_essence_text():
    s, cfg = _later_setup()
    ui = ScriptUI(["e", "keep the SECRETSAUCE name", "a"])
    r = AutoCompactController(s, cfg).evaluate(ui=ui, interactive=True, checkpoint=SAFE,
                                               apply_fn=lambda x: candidate_from(x))
    assert r["action"] in ("applied", "rejected"), r
    blob = json.dumps(s.meta.get("compactions", [])) + json.dumps(s.meta["auto_compaction"].get("history", []))
    assert "SECRETSAUCE" not in blob
    if r["action"] == "applied":
        assert s.meta["compactions"][-1]["user_action"] == "added_essence"


# ------------------------------------------------------------------ task 9
def test_assess_immediate_retrigger_blocker():
    s = big_session(10, chunk=200)
    cfg = sized(s, mkcfg(), 0.8)
    a = assess(s, cfg, keep_recent=50)  # nothing can be summarized
    assert any("immediately retrigger" in b for b in a.blockers) and a.recommendation == "skip"


def test_assess_protected_budget_blocker():
    s = Session(agent="g", project="p")
    s.messages = [msg("p" * 400, policy=Policy.KEEP_VERBATIM) for _ in range(5)] + [msg("tail")]
    cfg = sized(s, mkcfg(), 0.8)
    a = assess(s, cfg, keep_recent=1)
    assert any(b.startswith("protected content") for b in a.blockers) and a.recommendation == "skip"


def test_assess_failures_boundary_and_recommend_reasons():
    s = big_session(10, chunk=200)
    cfg = sized(s, mkcfg(), 0.8)
    ctrl = AutoCompactController(s, cfg)
    base = assess(s, cfg)
    assert any("natural task boundary" in r for r in base.reasons)
    assert any("above recommend threshold" in r for r in base.reasons)
    ctrl._meta["failed_attempts"] = 1
    after = assess(s, cfg)
    assert any("earlier automatic attempt" in r for r in after.reasons)


# ------------------------------------------------------------------ task 10
def test_review_functions():
    s = big_session(6, chunk=60)
    s.messages[3].tags.add("decision")
    p = compaction.plan(s, keep_recent=2)
    summary = review_summary(p, "medium")
    assert "KEEP:" in summary and "SUMMARIZE:" in summary and "RISK:      MEDIUM" in summary
    items = review_items(s, p)
    assert any(i.startswith("KEEP") and "goal" in i for i in items)
    assert any(i.startswith("SUM") for i in items)
    old = {"summary_high": {"open_tasks": ["Investigate parser"], "decisions": []}}
    new = {"summary_high": {"open_tasks": ["Fix parser"], "decisions": [{"what": "No regex"}]}}
    d = review_diff(old, new)
    assert "- Investigate parser" in d and "+ Fix parser" in d and "+ No regex" in d
    assert review_diff(old, None).startswith("(no candidate")
    assert review_diff(new, new) == "(no structural changes)"


def test_review_loop_back_then_apply():
    s, cfg = _later_setup()
    ui = ScriptUI(["r", "s", "i", "d", "b", "a"])
    r = AutoCompactController(s, cfg).evaluate(ui=ui, interactive=True, checkpoint=SAFE,
                                               apply_fn=lambda x: candidate_from(x))
    assert r["action"] in ("applied", "rejected"), r
    shown = "\n".join(ui.infos)
    assert "KEEP:" in shown and "SUM" in shown


# ------------------------------------------------------------------ task 11
def test_rollback_restores_messages():
    cfg = mkcfg(ask_user="never")
    s = big_session(6, chunk=200)
    ids = [m.id for m in s.messages]
    cfg = sized(s, cfg, 0.8)
    ctrl = AutoCompactController(s, cfg)
    assert ctrl.evaluate(interactive=False, checkpoint=SAFE, apply_fn=lambda x: candidate_from(x))["action"] == "applied"
    assert [m.id for m in s.messages] != ids
    assert ctrl.rollback() is True
    assert [m.id for m in s.messages] == ids
    assert not s.meta.get("compactions")
    assert ctrl.rollback() is False


def test_no_snapshot_when_rollback_disabled():
    cfg = mkcfg(ask_user="never", rollback_on_failure=False)
    s = big_session(6, chunk=200)
    cfg = sized(s, cfg, 0.8)
    assert run(s, cfg)["action"] == "applied"
    assert "rollback" not in s.meta["auto_compaction"]


def test_save_is_atomic(tmp_path, monkeypatch):
    s = big_session(2, chunk=20)
    store.save(s, tmp_path)
    path = store.store_path(tmp_path)
    before = path.read_text(encoding="utf-8")
    s.messages.append(msg("new"))

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store.os, "replace", boom)
    with pytest.raises(OSError):
        store.save(s, tmp_path)
    assert path.read_text(encoding="utf-8") == before


# ------------------------------------------------------------------ task 12
def test_force_warns_once_after_postpone():
    s, cfg = _later_setup()
    ctrl = AutoCompactController(s, cfg)
    ctrl.evaluate(ui=ScriptUI(["l"]), interactive=True, checkpoint=SAFE, apply_fn=lambda x: candidate_from(x))
    sized(s, cfg, 0.95)
    ui = ScriptUI([])
    ctrl.evaluate(ui=ui, interactive=True, checkpoint=SAFE, apply_fn=lambda x: candidate_from(x))
    warns = [i for i in ui.infos if "postponed" in i]
    assert len(warns) == 1


def test_noninteractive_postpone_but_critical_applies():
    cfg = mkcfg(ask_user="always", noninteractive_action="postpone")
    s = big_session(10, chunk=200)
    cfg = sized(s, cfg, 0.8)
    assert run(s, cfg)["action"] == "postponed_noninteractive"
    s = big_session(10, chunk=200)
    cfg = sized(s, cfg, 0.95)
    assert run(s, cfg)["action"] in ("applied", "rejected")

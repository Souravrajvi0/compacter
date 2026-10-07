import json
from pathlib import Path

import pytest

import ctxpipe.core.autocontrol as ac
from ctxpipe.config import AutoCompactConfig  # re-exported? no; import from module
from ctxpipe.core.autocontrol import (
    AutoCompactConfig, AutoCompactController, CompactionAssessment, StateMachine,
    TRANSITIONS, add_essence, assess, capture_checkpoint, expire_next_compaction,
    is_safe_boundary, parse_essence_input, pending_essences, repair, transcript_hash,
    verify,
)
from ctxpipe.core import compaction, store
from ctxpipe.core.models import Message, Policy, Session


def mkcfg(**kw):
    base = dict(enabled=True, context_window=2000, reserve_output_tokens=0, reserve_tool_tokens=0,
                min_turns_since_compaction=0, min_new_tokens=0, cooldown_turns=0,
                min_projected_reduction=0.2, min_reduction=0.2, evaluate_at_percent=65,
                recommend_at_percent=75, force_at_percent=90, rearm_below_percent=45,
                keep_recent=2)
    base.update(kw)
    return AutoCompactConfig(**base)


def msg(content, role="user", tags=None, policy=None, **kw):
    return Message(role, content, tags=tags or set(), policy=policy or Policy.SUMMARIZE_OK, **kw)


def sized(s: Session, cfg: AutoCompactConfig, ratio: float) -> AutoCompactConfig:
    """Point cfg.context_window so effective pressure ~= ratio."""
    from ctxpipe.core.tokens import estimate_tokens, session_tokens

    cur = session_tokens(s.visible()) + estimate_tokens(s.system)
    cfg.context_window = max(1, int(cur / ratio))
    return cfg


def big_session(n=6, chunk=400) -> Session:
    s = Session(agent="generic-api", project="p")
    s.messages = [msg("Goal: ship the parser", tags={"goal"}, policy=Policy.KEEP_VERBATIM),
                  msg("Constraint: no secrets", tags={"constraint"}, policy=Policy.KEEP_VERBATIM)]
    for i in range(n):
        s.messages.append(msg(f"filler {i} " + "x" * chunk, role="assistant"))
    s.messages.append(msg("all tests passed, step done", role="assistant"))
    return s


class FakeLLM:
    def complete(self, system, user, max_tokens=2048):
        return json.dumps({"summary_high": {"goals": ["Goal: ship the parser"], "constraints": ["Constraint: no secrets"],
                                            "decisions": [], "open_tasks": [], "key_files": []},
                           "summary_medium": "explored", "summary_low": ""})


def candidate_from(s: Session, drop_all: bool = False, essence_text: str | None = None) -> Session:
    p = compaction.plan(s, keep_recent=2)
    summary = compaction.summarize(p, FakeLLM())
    view = compaction.rebuild(s, p, summary)
    if drop_all:
        view.messages = [m for m in view.messages if "compaction_summary" in m.tags]
    if essence_text:
        for m in view.messages:
            if "compaction_summary" in m.tags:
                m.content += f"\n{essence_text}"
    return view


def essences_of(s):
    return pending_essences(s)


# 1
def test_no_action_below_65():
    cfg = mkcfg(context_window=1_000_000)
    s = big_session(2, chunk=10)
    result = AutoCompactController(s, cfg).evaluate(apply_fn=lambda x: candidate_from(x))
    assert result["action"] == "below_threshold"


# 2
def test_eligible_at_65():
    cfg = mkcfg(evaluate_at_percent=65, recommend_at_percent=75, force_at_percent=90)
    s = big_session(3, chunk=200)
    cfg = sized(s, cfg, 0.70)
    a = assess(s, cfg)
    assert a.effective_pressure >= cfg.evaluate_at_percent / 100


# 3
def test_skip_when_reduction_too_small():
    cfg = mkcfg()
    s = Session(agent="gpt", project="p")
    s.messages = [msg("a" * 300, policy=Policy.KEEP_VERBATIM) for _ in range(4)]
    cfg = sized(s, cfg, 0.9)
    result = AutoCompactController(s, cfg).evaluate(apply_fn=lambda x: candidate_from(x))
    assert result["action"] in ("skipped", "rejected")


# 4+6
def test_unsafe_boundary_open_pair_sets_pending():
    cfg = mkcfg()
    s = big_session(3, chunk=200)
    s.messages.append(Message("assistant", "[tool_use Bash: ls]", meta={"pair_ids": ["c1"]}))
    cfg = sized(s, cfg, 0.95)
    assert not is_safe_boundary(s)
    result = AutoCompactController(s, cfg).evaluate(apply_fn=lambda x: candidate_from(x))
    assert result["action"] == "pending_boundary"
    mm = s.meta["auto_compaction"]
    assert mm.get("compaction_pending") is True or AutoCompactController(s, cfg).machine.state == "PENDING_BOUNDARY"


# 5
def test_pending_resolves_on_safe_boundary():
    cfg = mkcfg()
    s = big_session(3, chunk=200)
    cfg = sized(s, cfg, 0.95)
    ctrl = AutoCompactController(s, cfg)
    s.messages.append(Message("assistant", "[tool_use Bash: ls]", meta={"pair_ids": ["c1"]}))
    r1 = ctrl.evaluate(apply_fn=lambda x: candidate_from(x))
    assert r1["action"] == "pending_boundary"
    s.messages.append(Message("tool", "file1", meta={"pair_ids": ["c1"]}))
    r2 = ctrl.evaluate(apply_fn=lambda x: candidate_from(x))
    assert r2["action"] in ("applied", "rejected", "skipped", "circuit_breaker", "postponed_noninteractive", "postponed_turns")


# 7
def test_smart_asks_on_medium_risk_via_ui():
    cfg = mkcfg(ask_user="smart")
    s = big_session(4, chunk=200)
    cfg = sized(s, cfg, 0.8)
    class Ui:
        def __init__(self): self.calls = 0
        def ask(self, t): self.calls += 1; return "l"
    ckpt = capture_checkpoint(s); ckpt["pending_steps"] = ["fix parser"]
    ui = Ui()
    result = AutoCompactController(s, cfg).evaluate(checkpoint=ckpt, ui=ui,
                                                    apply_fn=lambda x: candidate_from(x), interactive=True)
    assert ui.calls >= 1 and result["action"] == "postponed"


# 8
def test_smart_auto_applies_on_low_risk():
    cfg = mkcfg(ask_user="smart", min_projected_reduction=0.1, keep_recent=2)
    s = big_session(10, chunk=200)
    cfg = sized(s, cfg, 0.9)
    ckpt = capture_checkpoint(s); ckpt["pending_steps"] = []; ckpt["files_modified_by_user"] = []
    ckpt["next_action"] = "run tests"; ckpt["unresolved_questions"] = []
    result = AutoCompactController(s, cfg).evaluate(checkpoint=ckpt, interactive=False,
                                                    apply_fn=lambda x: candidate_from(x))
    assert result["action"] == "applied"


# 9
def test_always_mode_prompts():
    cfg = mkcfg(ask_user="always", min_projected_reduction=0.1, keep_recent=2)
    s = big_session(10, chunk=200)
    cfg = sized(s, cfg, 0.9)
    class Ui:
        def ask(self, t): return "a"
    result = AutoCompactController(s, cfg).evaluate(
        checkpoint={"phase": "testing", "pending_steps": [], "files_modified_by_user": [],
                    "next_action": "x", "unresolved_questions": []},
        ui=Ui(), apply_fn=lambda x: candidate_from(x), interactive=True)
    assert result["action"] == "applied"


def test_never_mode_auto_applies():
    cfg = mkcfg(ask_user="never", min_projected_reduction=0.1, keep_recent=2)
    s = big_session(10, chunk=200)
    cfg = sized(s, cfg, 0.9)
    result = AutoCompactController(s, cfg).evaluate(
        checkpoint={"phase": "testing", "pending_steps": [], "files_modified_by_user": [],
                    "next_action": "x", "unresolved_questions": []},
        interactive=False, apply_fn=lambda x: candidate_from(x))
    assert result["action"] in ("applied", "rejected")


def test_critical_only_only_asks_near_force():
    cfg = mkcfg(ask_user="critical-only", force_at_percent=90)
    s = big_session(3, chunk=200)
    cfg = sized(s, cfg, 0.95)
    result = AutoCompactController(s, cfg).evaluate(
        checkpoint={"phase": "testing", "pending_steps": ["x"], "files_modified_by_user": [],
                    "next_action": "x", "unresolved_questions": []},
        interactive=False, apply_fn=lambda x: candidate_from(x), ui=None)
    assert result["action"] != "postponed"


# 10
def test_essence_directive_creation():
    s = big_session(1, chunk=10)
    d = add_essence(s, "keep api field names")
    assert d["type"] == "essence_directive" and d["scope"] == "next_compaction" and d["policy"] == "must_preserve"
    assert pending_essences(s) == [d]


# 11
def test_essence_not_in_transcript_messages():
    s = big_session(1, chunk=10)
    add_essence(s, "x")
    assert all(not isinstance(m.content, dict) for m in s.messages)
    assert "essence_directive" not in [t for m in s.messages for t in m.tags]


# 12
def test_verify_fails_when_essence_missing():
    s = big_session(4, chunk=200)
    ess = [add_essence(s, "customer_external_id must be exact")]
    cand = candidate_from(s)
    v = verify(s, cand, ess, mkcfg(), reduction=0.5)
    assert not v.passed and any("essence" in e for e in v.errors)


# 13 + 14
def test_directive_survives_rejection_and_expires_on_success():
    cfg = mkcfg()
    s = big_session(6, chunk=200)
    cfg = sized(s, cfg, 0.9)
    d = add_essence(s, "keep the rejected async design")
    # rejection path: reduction below min -> verify fails
    ctrl = AutoCompactController(s, cfg)
    r = ctrl.evaluate(checkpoint=None, interactive=False,
                      apply_fn=lambda x: candidate_from(x), ui=None)
    assert r["action"] in ("rejected", "applied")
    if r["action"] == "rejected":
        assert d in pending_essences(s)


def test_directive_expires_after_successful_apply():
    cfg = mkcfg(ask_user="never")
    s = big_session(6, chunk=200)
    cfg = sized(s, cfg, 0.9)
    add_essence(s, "Goal: ship the parser")
    ctrl = AutoCompactController(s, cfg)
    r = ctrl.evaluate(interactive=False, apply_fn=lambda x: candidate_from(x, essence_text="Goal: ship the parser"))
    assert r["action"] == "applied"
    assert pending_essences(s) == []


# 15/16
def test_postpone_turns_and_tokens_markers():
    cfg = mkcfg(min_projected_reduction=0.1, keep_recent=2)
    s = big_session(10, chunk=200)
    cfg = sized(s, cfg, 0.8)
    ctrl = AutoCompactController(s, cfg)
    ctrl.machine.state = "BUILDING_CANDIDATE" if False else "IDLE"
    ctrl._meta["postpone"] = {"type": "turns", "amount": 5, "at": len(s.messages)}
    r = ctrl.evaluate(interactive=False, apply_fn=lambda x: candidate_from(x))
    assert r["action"] == "postponed_turns"
    ctrl._meta.pop("postpone")
    ctrl._meta["postpone"] = {"type": "tokens", "amount": 10_000, "at": 0}
    r2 = ctrl.evaluate(interactive=False, apply_fn=lambda x: candidate_from(x))
    assert r2["action"] == "postponed_tokens"


# 17
def test_session_disable_only_session():
    cfg = mkcfg(min_projected_reduction=0.1, keep_recent=2)
    s = big_session(10, chunk=200)
    cfg = sized(s, cfg, 0.9)
    ctrl = AutoCompactController(s, cfg)
    ctrl.set_disabled(True)
    r = ctrl.evaluate(interactive=False, apply_fn=lambda x: candidate_from(x))
    assert r["action"] == "disabled"
    assert cfg.enabled is True  # project config untouched


# 18
def test_noninteractive_never_blocks():
    cfg = mkcfg(ask_user="always", noninteractive_action="postpone", min_projected_reduction=0.1, keep_recent=2)
    s = big_session(10, chunk=200)
    cfg = sized(s, cfg, 0.8)
    result = AutoCompactController(s, cfg).evaluate(interactive=False, ui=None,
                                                    apply_fn=lambda x: candidate_from(x))
    assert result["action"] == "postponed_noninteractive"


# 19
def test_emergency_above_90_runs():
    cfg = mkcfg(force_at_percent=90, min_projected_reduction=0.1, keep_recent=2)
    s = big_session(10, chunk=200)
    cfg = sized(s, cfg, 0.95)
    ctrl = AutoCompactController(s, cfg)
    r = ctrl.evaluate(interactive=False, apply_fn=lambda x: candidate_from(x))
    assert r["action"] in ("applied", "rejected", "pending_boundary")


# 20
def test_checkpoint_has_required_fields():
    s = big_session(2, chunk=100)
    ckpt = capture_checkpoint(s)
    for key in ("objective", "phase", "action_permission", "active_step", "next_action",
                "stop_condition", "completed_steps", "pending_steps", "blocked_steps",
                "files_inspected", "files_modified_by_agent", "files_modified_by_user",
                "latest_tests", "known_errors", "rejected_approaches", "unresolved_questions",
                "last_successful_action", "resume_requires_confirmation"):
        assert key in ckpt


# 21
def test_completed_task_regression_detected():
    s = big_session(2, chunk=50)
    cand = candidate_from(s)
    cand.messages.append(msg("status: pending failed investigation"))
    v = verify(s, cand, [], mkcfg(), reduction=0.5)
    assert not v.passed and any("regression" in e for e in v.errors)


# 22
def test_exact_fact_verification():
    s = big_session(2, chunk=50)
    cand = candidate_from(s)
    ess = [add_essence(s, "customer_external_id")]
    v = verify(s, cand, ess, mkcfg(), reduction=0.5)
    assert not v.passed and any("essence" in e for e in v.errors)


# 23
def test_drop_always_leak_detected():
    s = big_session(2, chunk=50)
    s.messages.append(msg("sk-live-secret-123", policy=Policy.DROP_ALWAYS))
    cand = candidate_from(s)
    cand.messages.append(msg("here is sk-live-secret-123 again"))
    v = verify(s, cand, [], mkcfg(), reduction=0.5)
    assert any("drop_always" in e for e in v.errors)


# 24
def test_insufficient_reduction_rejected():
    s = big_session(2, chunk=50)
    cand = candidate_from(s)
    v = verify(s, cand, [], mkcfg(min_reduction=0.99), reduction=0.1)
    assert not v.passed and any("reduction" in e for e in v.errors)


# 25
def test_repair_fixes_missing_essence():
    s = big_session(4, chunk=100)
    ess = [add_essence(s, "customer_external_id exact")]
    cand = candidate_from(s)
    v1 = verify(s, cand, ess, mkcfg(), reduction=0.5)
    fixed = repair(cand, s, ess)
    v2 = verify(s, fixed, ess, mkcfg(), reduction=0.5)
    assert not v1.passed and v2.passed


# 26
def test_circuit_breaker_max_attempts():
    cfg = mkcfg()
    s = big_session(4, chunk=100)
    cfg = sized(s, cfg, 0.9)
    ctrl = AutoCompactController(s, cfg)
    ctrl._meta["failed_attempts"] = 2
    r = ctrl.evaluate(interactive=False, apply_fn=lambda x: candidate_from(x))
    assert r["action"] == "circuit_breaker"


# 27
def test_cooldown_blocks_reevaluation():
    cfg = mkcfg(cooldown_turns=10)
    s = big_session(6, chunk=200)
    cfg = sized(s, cfg, 0.85)
    ctrl = AutoCompactController(s, cfg)
    ctrl.note_success(transcript_hash(s), len(s.messages))
    r = ctrl.evaluate(interactive=False, apply_fn=lambda x: candidate_from(x))
    assert r["action"] == "cooldown"


# 28
def test_state_persists_across_controller_instances():
    cfg = mkcfg()
    s = big_session(2, chunk=50)
    c1 = AutoCompactController(s, cfg)
    c1.machine.try_transition("WATCHING"); c1._persist()
    c2 = AutoCompactController(s, cfg)
    assert c2.machine.state == "WATCHING"


# 29
def test_audit_contains_no_secret():
    cfg = mkcfg(ask_user="never")
    s = big_session(6, chunk=200)
    cfg = sized(s, cfg, 0.9)
    s.messages.append(msg("sk-live-DO-NOT-LEAK", policy=Policy.DROP_ALWAYS))
    ctrl = AutoCompactController(s, cfg)
    r = ctrl.evaluate(interactive=False, apply_fn=lambda x: candidate_from(x))
    blob = json.dumps(s.meta.get("compactions", []))
    assert "sk-live-DO-NOT-LEAK" not in blob


# 30
def test_disabled_without_config():
    cfg = AutoCompactConfig(enabled=False)  # backward compat default
    s = big_session(6, chunk=200)
    r = AutoCompactController(s, cfg).evaluate(interactive=False, apply_fn=lambda x: candidate_from(x))
    assert r["action"] == "disabled"


# 31/32
def test_invalid_thresholds_raise():
    with pytest.raises(ValueError):
        AutoCompactConfig(enabled=True, evaluate_at_percent=80, recommend_at_percent=70, force_at_percent=90, rearm_below_percent=45).validate()
    with pytest.raises(ValueError):
        AutoCompactConfig(enabled=True, evaluate_at_percent=65, recommend_at_percent=75, force_at_percent=90, rearm_below_percent=70).validate()


# 33
def test_candidate_above_threshold_rejected():
    cfg = mkcfg(context_window=100)
    s = big_session(6, chunk=200)
    cand = s  # candidate still huge
    v = verify(s, cand, [], cfg, reduction=0.5)
    assert any("above force threshold" in e for e in v.errors)


# 34
def test_repeated_source_hash_circuit():
    cfg = mkcfg()
    s = big_session(4, chunk=100)
    ctrl = AutoCompactController(s, cfg)
    h = transcript_hash(s)
    ctrl._meta["last_fail_hash"] = h
    ctrl._meta["failed_attempts"] = 2
    blocked, why = ctrl.circuit_open(h)
    assert blocked


# 35
def test_parse_direct_keep_apply():
    directive, action = parse_essence_input("keep exact migration names and the rejected async design; apply")
    assert directive is not None and "migration" in directive and action == "apply"
    directive2, action2 = parse_essence_input("keep the regex parser rejection")
    assert directive2 and action2 == "question"


# extra: audit record shape
def test_audit_entry_has_automatic_fields():
    cfg = mkcfg(ask_user="never")
    s = big_session(6, chunk=200)
    cfg = sized(s, cfg, 0.9)
    ctrl = AutoCompactController(s, cfg)
    ctrl.evaluate(interactive=False, apply_fn=lambda x: candidate_from(x))
    assert s.meta["compactions"], "audit entry missing"
    e = s.meta["compactions"][-1]
    for k in ("automatic", "trigger_percent", "effective_pressure", "assessment",
              "user_action", "essence_directive_ids", "verification", "state_before", "state_after"):
        assert k in e


# state machine legality
def test_state_machine_rejects_illegal():
    m = StateMachine("IDLE")
    with pytest.raises(ValueError):
        m.transition("APPLIED")
    assert m.try_transition("WATCHING") is True
    assert m.try_transition("APPLIED") is False


def test_transitions_table_covers_expected():
    assert "EMERGENCY" in TRANSITIONS["WATCHING"]
    assert "EMERGENCY" in TRANSITIONS["VERIFYING"]
    assert "APPLIED" in TRANSITIONS["VERIFYING"]
    assert "COOLDOWN" in TRANSITIONS["APPLIED"]

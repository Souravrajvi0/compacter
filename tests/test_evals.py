from codec import evals
from codec.core.models import Message, Policy, Session


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete(self, system, user, max_tokens=2048):
        self.calls.append((system, user))
        return self.reply


def make_view() -> Session:
    s = Session(agent="generic-api", project="x")
    s.messages = [
        Message("user", "goal: fix auth bug", tags={"goal"}, policy=Policy.KEEP_VERBATIM),
        Message("user", "api key sk-secret", policy=Policy.DROP_ALWAYS),
        Message("assistant", "patched token check"),
    ]
    return s


def test_judge_pass_and_fail():
    view = make_view()
    ok = evals.judge(view, "fix the auth bug", FakeLLM("YES\ngoal and fix are present"))
    assert ok.passed and "goal" in ok.explanation
    bad = evals.judge(view, "fix the auth bug", FakeLLM("NO\ncontext is gone"))
    assert not bad.passed


def test_judge_prompt_excludes_drop_always():
    view = make_view()
    llm = FakeLLM("YES\nok")
    evals.judge(view, "task", llm)
    user_prompt = llm.calls[0][1]
    assert "sk-secret" not in user_prompt
    assert "fix auth bug" in user_prompt

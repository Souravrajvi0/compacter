import io
import json
from pathlib import Path

import pytest

from ctxpipe import hook
from ctxpipe.core import store
from ctxpipe.core.models import Message, Session


class FakeAdapter:
    def __init__(self):
        self.written = None

    def read(self, project):
        return Session(agent="claude-code", project=str(project),
                       messages=[Message(role="user" if i % 2 == 0 else "assistant", content=f"message {i}")
                                 for i in range(12)])

    def write(self, session, project):
        self.written = session
        return Path(project) / "abc123.jsonl"


class StubLLM:
    def complete(self, system, user, max_tokens=2048):
        return "stub summary"


@pytest.fixture
def env(tmp_path, monkeypatch):
    fa = FakeAdapter()
    monkeypatch.setattr(hook, "get_adapter", lambda name: fa)
    monkeypatch.setattr("ctxpipe.llm.get_llm", lambda name="auto", **kw: StubLLM())
    return tmp_path, fa


def test_ignores_other_prompts(env):
    project, _ = env
    assert hook.respond("fix the bug", project, "claude-code") is None


def test_list_shows_checkmarks_on_right(env):
    project, _ = env
    out = hook.respond("/compacter", project, "claude-code")
    assert "6 messages can be compacted" in out
    rows = [l for l in out.splitlines() if l.endswith("[✓]")]
    assert len(rows) == 6 and "message 0" in rows[0]
    assert store.load(project).meta["picker_listed"] is True


def test_skip_keeps_those_verbatim(env):
    project, fa = env
    hook.respond("/compacter", project, "claude-code")
    out = hook.respond("/compacter skip 0-1", project, "claude-code")
    assert "Compacted 4 messages" in out and "claude --resume abc123" in out
    kept = [m.content for m in fa.written.messages]
    assert "message 0" in kept and "message 1" in kept and "message 2" not in kept


def test_only_and_bad_args(env):
    project, fa = env
    assert "Compacted 2 messages" in hook.respond("/compacter only 2,3", project, "claude-code")
    assert "out of bounds" in hook.respond("/compacter only 99", project, "claude-code")
    assert "Unknown option" in hook.respond("/compacter wat", project, "claude-code")


def test_run_emits_block_decision(env, monkeypatch, capsys):
    project, _ = env
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "/compacter", "cwd": str(project)})))
    assert hook.run("claude-code") == 0
    d = json.loads(capsys.readouterr().out)
    assert d["decision"] == "block" and "can be compacted" in d["reason"]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "hello", "cwd": str(project)})))
    hook.run("claude-code")
    assert capsys.readouterr().out == ""


def test_pack_roundtrip_via_hook(env, monkeypatch, tmp_path):
    from ctxpipe.core import compaction, packs
    from ctxpipe.core.models import Message, Session

    monkeypatch.setenv("CTXPIPE_HOME", str(tmp_path / "home"))
    project, fa = env
    s = Session(agent="claude-code", project=str(project), messages=[
        Message(role="user", content=f"m{i}") for i in range(8)])
    p = compaction.plan(s, keep_recent=2)
    store.save(compaction.rebuild(s, p, '{"summary_high":{"goals":["g1"],"constraints":[],"decisions":[],'
                                       '"open_tasks":[],"key_files":[]},"summary_medium":"x","summary_low":""}'), project)
    assert "Saved pack 'demo'" in hook.respond("/compacter pack save demo", project, "claude-code")
    assert "demo" in hook.respond("/compacter pack list", project, "claude-code")
    loaded = hook.respond("/compacter pack load demo", project, "claude-code")
    assert isinstance(loaded, hook.Inject) and "g1" in loaded
    assert "claude --resume" in hook.respond("/compacter pack new demo", project, "claude-code")
    assert "No pack named" in hook.respond("/compacter pack delete nope", project, "claude-code")
    assert "no pack named" in hook.respond("/compacter pack load nope", project, "claude-code").lower()


def test_run_injects_context_for_pack_load(env, monkeypatch, capsys, tmp_path):
    from ctxpipe.core import packs

    monkeypatch.setenv("CTXPIPE_HOME", str(tmp_path / "home"))
    (packs.pack_dir()).mkdir(parents=True)
    (packs.pack_dir() / "demo.json").write_text(json.dumps(
        {"name": "demo", "summary_text": "CODEWORD-1", "summary_json": None, "pinned": []}), encoding="utf-8")
    project, _ = env
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "/compacter pack load demo", "cwd": str(project)})))
    hook.run("claude-code")
    out = json.loads(capsys.readouterr().out)
    assert "decision" not in out
    assert "CODEWORD-1" in out["hookSpecificOutput"]["additionalContext"]

"""Edge-case hardening tests."""
from pathlib import Path

import compacter.adapters.aider as aider
from compacter.core import compaction, store, tokens
from compacter.core.models import Message, Policy, Session


def test_compaction_all_keep_verbatim():
    s = Session(agent="generic-api", project="x")
    s.messages = [Message("user", f"m{i}", policy=Policy.KEEP_VERBATIM) for i in range(5)]
    p = compaction.plan(s, keep_recent=2)
    assert p.to_summarize == [] and p.dropped == []
    summary = compaction.summarize(p, llm=None)
    assert summary == ""
    view = compaction.rebuild(s, p, summary)
    assert [m.content for m in view.messages] == [f"m{i}" for i in range(5)]
    assert compaction.tokens_after(view) > 0


def test_compaction_empty_session():
    s = Session(agent="generic-api", project="x")
    p = compaction.plan(s)
    assert p.pinned == [] and p.recent == [] and p.to_summarize == []
    view = compaction.rebuild(s, p, "")
    assert view.messages == []


def test_double_compaction_audit_trail(tmp_path: Path, monkeypatch):
    import compacter.cli as cli

    class FakeAdapter:
        name = "generic-api"

        def write(self, session, project):
            return project / "out.json"

    class FakeLLM:
        def complete(self, system, user, max_tokens=2048):
            return "summary"

    s = Session(agent="generic-api", project=str(tmp_path))
    s.messages = [Message("user", f"msg {i}") for i in range(8)]
    store.save(s, tmp_path)
    monkeypatch.setattr(cli, "get_adapter", lambda name: FakeAdapter())
    monkeypatch.setattr("compacter.llm.get_llm", lambda p="auto", **kw: FakeLLM())

    assert cli.main(["--project", str(tmp_path), "compact", "--apply", "--keep-recent", "3"]) == 0
    loaded = store.load(tmp_path)
    loaded.messages.append(Message("user", "more work"))
    store.save(loaded, tmp_path)
    assert cli.main(["--project", str(tmp_path), "compact", "--apply", "--keep-recent", "3"]) == 0
    final = store.load(tmp_path)
    assert len(final.meta["compactions"]) == 2
    assert final.meta["compactions"][0]["summarized"] >= 0


def test_tokenizer_fallback_no_tiktoken(monkeypatch):
    monkeypatch.setattr(tokens, "_ENC", None)
    assert tokens.estimate_tokens("hello world, this is a test") >= 1
    assert tokens.estimate_tokens("") == 0
    msgs = [Message("user", "abcd" * 10)]
    assert tokens.session_tokens(msgs) >= 1


def test_aider_missing_file(tmp_path: Path):
    import pytest

    with pytest.raises(FileNotFoundError):
        aider.AiderAdapter().read(tmp_path)


def test_aider_crlf_and_nul_bytes(tmp_path: Path):
    data = b"#### user\r\n\r\nhello\r\n\r\n#### assistant\r\n\r\nwith weird \xff byte\r\n"
    (tmp_path / ".aider.chat.history.md").write_bytes(data)
    s = aider.AiderAdapter().read(tmp_path)
    assert [m.role for m in s.messages] == ["user", "assistant"]
    assert "hello" in s.messages[0].content

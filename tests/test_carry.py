import json

import pytest

from compacter import cli, hook
from compacter.carry import carry
from compacter.core import packs
from compacter.core.models import Message, Session

SUMMARY = json.dumps({
    "summary_high": {"goals": ["carry-goal"], "constraints": [], "decisions": [], "open_tasks": [], "key_files": []},
    "summary_medium": "m", "summary_low": "",
})


class StubLLM:
    seen = ""

    def complete(self, system, user, max_tokens=2048):
        StubLLM.seen = user
        return SUMMARY


def _chat(n=10):
    return Session(agent="generic-api", project=".",
                   messages=[Message(role="user" if i % 2 == 0 else "assistant", content=f"line {i}") for i in range(n)])


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPACTER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("compacter.llm.get_llm", lambda name="auto", **kw: StubLLM())


def test_verbatim_carry_keeps_only_chosen(tmp_path):
    s = _chat()
    res = carry(s, {s.messages[2].id, s.messages[3].id}, tmp_path, name="v", verbatim=True)
    pack = packs.load_pack("v")
    assert [p["content"] for p in pack["pinned"]] == ["line 2", "line 3"] and res.count == 2
    assert pack["summary_json"] is None
    assert "line 2" in packs.render_markdown(pack) and "line 5" not in packs.render_markdown(pack)


def test_summarized_carry_sends_only_chosen_to_llm(tmp_path):
    s = _chat()
    carry(s, {s.messages[4].id}, tmp_path, name="s")
    assert "line 4" in StubLLM.seen and "line 5" not in StubLLM.seen and "line 0" not in StubLLM.seen
    assert "carry-goal" in packs.render_markdown(packs.load_pack("s"))


def test_new_chat_written_and_source_untouched(tmp_path):
    s = _chat()
    before = [m.content for m in s.messages]
    res = carry(s, {s.messages[1].id}, tmp_path, name="n", verbatim=True, new_chat=True)
    assert res.new_chat is not None and "line 1" in res.new_chat.read_text(encoding="utf-8")
    assert [m.content for m in s.messages] == before


def test_nothing_selected_is_an_error(tmp_path):
    with pytest.raises(ValueError):
        carry(_chat(), set(), tmp_path)


def test_cli_carry_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_adapter", lambda name: type("A", (), {"read": lambda self, p: _chat(),
                                                                         "write": lambda self, s, p: tmp_path / "x"})())
    assert cli.main(["--project", str(tmp_path), "carry", "--agent", "generic-api", "--only", "2-3",
                     "--verbatim", "--name", "viacli"]) == 0
    assert "carried 2 messages (verbatim)" in capsys.readouterr().out
    assert cli.main(["--project", str(tmp_path), "carry", "--agent", "generic-api", "--only", "99"]) == 1


class FakeAdapter:
    def read(self, project):
        return _chat()

    def write(self, session, project):
        return project / "abc.jsonl"


def test_hook_carry_list_and_pick(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "get_adapter", lambda name: FakeAdapter())
    listing = hook.respond("/compacter carry", tmp_path, "claude-code")
    assert sum(l.endswith("[ ]") for l in listing.splitlines()) == 10 and "[✓]" not in listing
    out = hook.respond("/compacter carry 2-3 verbatim as mine", tmp_path, "claude-code")
    assert "Carried 2 messages (word for word) into pack 'mine'" in out and "pack load mine" in out
    out2 = hook.respond("/compacter carry 1 verbatim new", tmp_path, "claude-code")
    assert "claude --resume" in out2
    assert "Unknown option" in hook.respond("/compacter carry banana", tmp_path, "claude-code")


def test_unstructured_summary_is_flagged(tmp_path, monkeypatch):
    class Prose:
        def complete(self, system, user, max_tokens=2048):
            return "just some prose, not JSON"

    monkeypatch.setattr("compacter.llm.get_llm", lambda name="auto", **kw: Prose())
    s = _chat()
    bad = carry(s, {s.messages[0].id}, tmp_path, name="bad")
    assert bad.structured is False
    assert "just some prose" in packs.render_markdown(packs.load_pack("bad"))
    assert carry(s, {s.messages[0].id}, tmp_path, name="v", verbatim=True).structured is True

import json

import pytest

from compacter.adapters import get_adapter
from compacter.core import compaction, packs, store
from compacter.core.models import Message, Session
from compacter.core.summary import parse_structured_summary

SUMMARY = json.dumps({
    "summary_high": {"goals": ["ship the login fix"], "constraints": ["no new deps"],
                     "decisions": [{"what": "use argon2", "why": "security", "alternatives_rejected": []}],
                     "open_tasks": ["add tests"], "key_files": ["auth.py"]},
    "summary_medium": "Investigated auth flow.", "summary_low": "",
})


class StubLLM:
    def complete(self, system, user, max_tokens=2048):
        return SUMMARY


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPACTER_HOME", str(tmp_path / "home"))


def _compacted() -> Session:
    s = Session(agent="opencode", project="proj", messages=[
        Message(role="user", content="Goal: fix login", tags={"goal"}),
        *[Message(role="user" if i % 2 else "assistant", content=f"turn {i}") for i in range(10)],
    ])
    p = compaction.plan(s, keep_recent=2)
    return compaction.rebuild(s, p, compaction.summarize(p, StubLLM()))


def test_save_requires_a_compaction():
    s = Session(agent="x", project=".", messages=[Message(role="user", content="hi")])
    with pytest.raises(ValueError, match="no compaction"):
        packs.save_pack("a", s)


def test_save_load_list_delete():
    packs.save_pack("login-fix", _compacted(), note="auth work")
    pack = packs.load_pack("login-fix")
    assert pack["source"]["agent"] == "opencode" and pack["note"] == "auth work"
    assert [p["content"] for p in pack["pinned"]] == ["Goal: fix login"]
    assert [r["name"] for r in packs.list_packs()] == ["login-fix"]
    assert packs.delete_pack("login-fix") and not packs.delete_pack("login-fix")
    with pytest.raises(FileNotFoundError):
        packs.load_pack("login-fix")


def test_bad_names_rejected():
    for bad in ("", "../x", "a/b", "x" * 80):
        with pytest.raises(ValueError):
            packs.load_pack(bad)


def test_seed_new_chat_contains_summary_and_pins(tmp_path):
    packs.save_pack("p", _compacted())
    seeded = packs.seed_session(packs.load_pack("p"), "generic-api", tmp_path)
    first = seeded.messages[0].content
    assert "ship the login fix" in first and "argon2" in first and "Goal: fix login" in first
    assert seeded.messages[-1].role == "assistant"
    out = get_adapter("generic-api").write(seeded, tmp_path)
    assert "ship the login fix" in out.read_text(encoding="utf-8")


def test_seeded_chat_chains_into_next_compaction(tmp_path):
    packs.save_pack("p", _compacted())
    seeded = packs.seed_session(packs.load_pack("p"), "generic-api", tmp_path)
    prev = parse_structured_summary(seeded.messages[0].meta["summary_json"])
    assert prev["summary_high"]["goals"] == ["ship the login fix"]
    for i in range(10):
        seeded.messages.append(Message(role="user", content=f"new work {i}"))
    p = compaction.plan(seeded, keep_recent=2)
    assert seeded.messages[0] in p.to_summarize or seeded.messages[0] in p.pinned


def test_cli_pack_and_new(tmp_path, capsys, monkeypatch):
    from compacter import cli

    proj = tmp_path / "proj"
    proj.mkdir()
    store.save(_compacted(), proj)
    assert cli.main(["--project", str(proj), "pack", "save", "mypack"]) == 0
    assert cli.main(["pack", "list"]) == 0
    assert "mypack" in capsys.readouterr().out
    assert cli.main(["--project", str(proj), "new", "--agent", "generic-api", "--from", "mypack"]) == 0
    assert "seeded with pack 'mypack'" in capsys.readouterr().out
    assert cli.main(["--project", str(proj), "new", "--agent", "generic-api", "--from", "nope"]) == 1

from pathlib import Path

from ctxpipe.core import compaction, store
from ctxpipe.core.models import Message, Policy, Session


class FakeLLM:
    def __init__(self):
        self.calls = []

    def complete(self, system, user, max_tokens=2048):
        self.calls.append(user)
        return "## Progress\nsummary"


def make_session() -> Session:
    s = Session(agent="generic-api", project="x")
    s.messages = [
        Message("user", "build a CLI", tags={"goal"}),
        Message("assistant", "ok, working on it"),
        Message("user", "secret key abc", policy=Policy.DROP_ALWAYS),
        Message("assistant", "noise", policy=Policy.DROP_OK),
        Message("user", "must use python", policy=Policy.KEEP_VERBATIM),
        Message("assistant", "recent 1"),
        Message("user", "recent 2"),
    ]
    return s


def test_plan_buckets():
    p = compaction.plan(make_session(), keep_recent=2)
    assert [m.content for m in p.pinned] == ["build a CLI", "must use python"]
    assert [m.content for m in p.to_summarize] == ["ok, working on it"]
    assert [m.content for m in p.dropped] == ["noise"]
    assert [m.content for m in p.excluded] == ["secret key abc"]
    assert [m.content for m in p.recent] == ["recent 1", "recent 2"]


def test_drop_always_never_reaches_llm_or_output():
    s, llm = make_session(), FakeLLM()
    p = compaction.plan(s, keep_recent=2)
    view = compaction.rebuild(s, p, compaction.summarize(p, llm))
    assert "secret key" not in llm.calls[0]
    assert all("secret key" not in m.content for m in view.messages)
    assert "noise" not in llm.calls[0]


def test_rebuild_order_and_master_untouched():
    s = make_session()
    p = compaction.plan(s, keep_recent=2)
    view = compaction.rebuild(s, p, "SUM")
    assert [m.content for m in view.messages][:2] == ["build a CLI", "must use python"]
    assert "SUM" in view.messages[2].content
    assert view.messages[-1].content == "recent 2"
    assert len(s.messages) == 7


def test_master_transcript_updated_after_apply(tmp_path: Path, monkeypatch):
    import ctxpipe.cli as cli

    s = make_session()
    store.save(s, tmp_path)

    class FakeAdapter:
        name = "generic-api"

        def write(self, session, project):
            return project / "out.json"

    monkeypatch.setattr(cli, "get_adapter", lambda name: FakeAdapter())
    monkeypatch.setattr("ctxpipe.llm.get_llm", lambda p="auto", **kw: FakeLLM())

    rc = cli.main(["--project", str(tmp_path), "compact", "--apply", "--keep-recent", "2"])
    assert rc == 0
    loaded = store.load(tmp_path)
    assert len(loaded.messages) == 5  # 2 pinned + summary + 2 recent
    assert loaded.messages[2].tags == {"compaction_summary"}
    assert "compactions" in loaded.meta and len(loaded.meta["compactions"]) == 1
    contents = [m.content for m in loaded.messages]
    assert "ok, working on it" not in contents  # summarized away


def test_threshold_skips_compaction(tmp_path: Path, monkeypatch, capsys):
    import ctxpipe.cli as cli

    s = make_session()
    store.save(s, tmp_path)
    monkeypatch.setattr(cli, "get_adapter", lambda name: (_ for _ in ()).throw(AssertionError("should not write")))
    rc = cli.main(["--project", str(tmp_path), "compact", "--apply", "--threshold", "100000"])
    assert rc == 0
    assert "nothing to do" in capsys.readouterr().out
    loaded = store.load(tmp_path)
    assert len(loaded.messages) == len(s.messages)


def test_tool_pair_not_split_across_summary_boundary():
    s = Session(agent="generic-api", project="x")
    s.messages = [
        Message("user", "do a thing"),
        Message("assistant", "calling tool", meta={"pair_ids": ["call_1"]}),
        Message("tool", "tool output", meta={"pair_ids": ["call_1"]}),
        Message("user", "thanks"),
        Message("assistant", "done"),
    ]
    p = compaction.plan(s, keep_recent=3)
    recent_contents = [m.content for m in p.recent]
    assert "calling tool" in recent_contents
    assert "tool output" in recent_contents
    assert "calling tool" not in [m.content for m in p.to_summarize]


def test_store_roundtrip(tmp_path: Path):
    s = make_session()
    store.save(s, tmp_path)
    loaded = store.load(tmp_path)
    assert [m.to_dict() for m in loaded.messages] == [m.to_dict() for m in s.messages]

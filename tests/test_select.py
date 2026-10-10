import pytest

from ctxpipe.core import compaction
from ctxpipe.core.models import Message, Session
from ctxpipe.tui import DOWN, ENTER, SPACE, Picker, parse_ranges, pick_messages


def _session(n=10):
    return Session(agent="generic", project=".",
                   messages=[Message(role="user" if i % 2 == 0 else "assistant", content=f"msg {i}")
                             for i in range(n)])


def test_parse_ranges():
    assert parse_ranges("0,3-5,-1", 10) == {0, 3, 4, 5, 9}
    with pytest.raises(ValueError):
        parse_ranges("12", 10)


def test_plan_only_ids_keeps_unchecked_verbatim():
    s = _session()
    chosen = {s.messages[1].id, s.messages[2].id}
    p = compaction.plan(s, keep_recent=2, only_ids=chosen)
    assert {m.id for m in p.to_summarize} == chosen
    assert len(p.pinned) == 6 and len(p.recent) == 2


def test_picker_toggle_all_none_and_confirm():
    s = _session(4)
    pk = Picker(s.messages, [0, 1, 2, 3], {m.id for m in s.messages})
    pk.handle(SPACE)                       # uncheck row 0
    assert s.messages[0].id not in pk.selected
    pk.handle(DOWN); pk.handle(SPACE)      # uncheck row 1
    assert len(pk.selected) == 2
    pk.handle("n"); assert not pk.selected
    pk.handle("a"); assert len(pk.selected) == 4
    assert pk.handle(ENTER) == "confirm" and pk.handle("q") == "cancel"
    assert any("[✓]" in line for line in pk.render())


def test_toggle_moves_tool_pair_together():
    a = Message(role="assistant", content="call", meta={"pair_id": "t1"})
    b = Message(role="tool", content="result", meta={"pair_id": "t1"})
    pk = Picker([a, b], [0, 1], {a.id, b.id})
    pk.handle(SPACE)
    assert pk.selected == set()


def test_pick_messages_cancel_and_confirm(capsys):
    s = _session(3)
    keys = iter([SPACE, ENTER])
    got = pick_messages(s.messages, [0, 1, 2], {m.id for m in s.messages}, read=lambda: next(keys))
    assert got == {s.messages[1].id, s.messages[2].id}
    assert pick_messages(s.messages, [0, 1, 2], set(), read=iter(["q"]).__next__) is None

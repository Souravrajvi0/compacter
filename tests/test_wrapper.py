import io
import sys
from pathlib import Path

import pytest

from ctxpipe import wrapper
from ctxpipe.core import store
from ctxpipe.core.models import Message, Session


def test_is_compact_command():
    assert wrapper.is_compact_command("/compact")
    assert wrapper.is_compact_command("  /compact  ")
    assert not wrapper.is_compact_command("/compact now")
    assert not wrapper.is_compact_command("hello")


class FakeLLM:
    def complete(self, system, user, max_tokens=2048):
        return "## Progress\nsummary"


def test_wrap_intercepts_compact(tmp_path: Path, monkeypatch, capsys):
    s = Session(agent="generic-api", project=str(tmp_path))
    s.messages = [Message("user", f"m{i}") for i in range(10)]
    store.save(s, tmp_path)
    monkeypatch.setattr("ctxpipe.llm.get_llm", lambda name: FakeLLM())

    echo = [sys.executable, "-u", "-c",
            "import sys\nfor line in sys.stdin:\n    print('echo:', line, end='')\n    sys.stdout.flush()"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello\n/compact\n"))
    rc = wrapper.run(tmp_path, "generic-api", echo, keep_recent=2)
    out = capsys.readouterr().out
    assert "echo: hello" in out
    assert "[ctxpipe] /compact intercepted" in out
    loaded = store.load(tmp_path)
    assert len(loaded.messages) == 3  # summary + 2 recent
    assert (tmp_path / "messages.compacted.json").exists()
    assert rc is not None


def test_wrap_passthrough_exit(tmp_path: Path, monkeypatch):
    echo = [sys.executable, "-u", "-c", "pass"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert wrapper.run(tmp_path, "generic-api", echo) == 0

"""Integration tests: each adapter reads a real-format session fixture."""
from pathlib import Path

import ctxpipe.adapters.claude_code as claude_code
import ctxpipe.adapters.codex as codex
from ctxpipe.adapters import claude_code as cc_mod, get_adapter

DATA = Path(__file__).parent / "data"


def test_claude_code_integration(tmp_path: Path, monkeypatch):
    session_dir = tmp_path / "claude-home" / cc_mod.encode_project_dir(tmp_path)
    session_dir.mkdir(parents=True)
    (session_dir / "abc.jsonl").write_text(
        (DATA / "claude_sample.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(claude_code, "CLAUDE_HOME", tmp_path / "claude-home")
    s = get_adapter("claude-code").read(tmp_path)
    roles = [m.role for m in s.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    pair = [m for m in s.messages if m.pair_ids()]
    assert len(pair) == 2 and pair[0].pair_ids() == pair[1].pair_ids()
    assert "on it" in s.messages[1].content


def test_codex_integration(tmp_path: Path, monkeypatch):
    day = tmp_path / "codex-home" / "2026" / "10" / "01"
    day.mkdir(parents=True)
    import json

    text = (DATA / "codex_rollout.jsonl").read_text(encoding="utf-8").replace(
        "@PROJECT@", json.dumps(str(tmp_path))[1:-1])
    (day / "rollout-x.jsonl").write_text(text, encoding="utf-8")
    monkeypatch.setattr(codex, "CODEX_HOME", tmp_path / "codex-home")
    s = get_adapter("codex").read(tmp_path)
    roles = [m.role for m in s.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert s.messages[2].pair_ids() == s.messages[1].pair_ids()
    assert "two files" in s.messages[3].content


def test_aider_integration(tmp_path: Path):
    (tmp_path / ".aider.chat.history.md").write_text(
        (DATA / "aider_sample.md").read_text(encoding="utf-8"), encoding="utf-8")
    s = get_adapter("aider").read(tmp_path)
    assert [m.content for m in s.messages][0] == "what time is it"
    assert s.messages[-1].content.startswith("truncated final")
    out = get_adapter("aider").write(s, tmp_path)
    assert out.name.endswith(".compacted.md")
    assert (tmp_path / ".aider.chat.history.md").stat().st_size > 0

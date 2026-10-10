import io
import json
import subprocess

import pytest

from codec import hook
from codec.llm import base, cli_llm


def _done(out="", err="", code=0):
    return subprocess.CompletedProcess([], code, out, err)


def test_auto_prefers_the_agent_in_use(monkeypatch):
    monkeypatch.setattr(cli_llm, "available", lambda kind: True)
    assert base.get_llm("auto", agent="codex").kind == "codex"
    assert base.get_llm("auto", agent="opencode").kind == "opencode"
    assert base.get_llm("auto", agent="aider").kind == "claude"      # no CLI of its own -> first installed


def test_auto_falls_back_to_api_key_then_errors(monkeypatch):
    monkeypatch.setattr(cli_llm, "available", lambda kind: False)
    for k in ("ANTHROPIC_API_KEY", "CODEC_ANTHROPIC_KEYS"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="no summarizer"):
        base.get_llm("auto", agent="claude-code")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr("codec.llm.anthropic_llm.AnthropicLLM", lambda: "api")
    assert base.get_llm("auto") == "api"


def test_claude_call_is_isolated_and_nested_marked(monkeypatch):
    seen = {}

    def fake_run(cmd, cwd, stdin):
        seen.update(cmd=cmd, cwd=cwd, stdin=stdin)
        return _done("  the summary \n")

    monkeypatch.setattr(cli_llm, "_run", fake_run)
    monkeypatch.setattr(cli_llm, "_resolve", lambda k: ["claude"])
    assert cli_llm.AgentLLM("claude").complete("SYS", "USER") == "the summary"
    assert "--no-session-persistence" in seen["cmd"] and "SYS" in seen["cmd"] and seen["stdin"] == "USER"


def test_codex_reads_last_message_file(monkeypatch):
    def fake_run(cmd, cwd, stdin):
        out = cmd[cmd.index("-o") + 1]
        open(out, "w", encoding="utf-8").write("from codex")
        return _done("noise")

    monkeypatch.setattr(cli_llm, "_run", fake_run)
    monkeypatch.setattr(cli_llm, "_resolve", lambda k: ["codex"])
    assert cli_llm.AgentLLM("codex").complete("s", "u") == "from codex"


def test_opencode_json_events(monkeypatch):
    ok = "\n".join(json.dumps(e) for e in [{"type": "step_start"}, {"type": "text", "part": {"text": "hello "}},
                                           {"type": "text", "part": {"text": "world"}}])
    monkeypatch.setattr(cli_llm, "_resolve", lambda k: ["opencode"])
    monkeypatch.setattr(cli_llm, "_run", lambda c, d, s: _done(ok))
    assert cli_llm.AgentLLM("opencode").complete("s", "u") == "hello world"
    bad = json.dumps({"type": "error", "error": {"name": "APIError", "data": {"message": "rate limited"}}})
    monkeypatch.setattr(cli_llm, "_run", lambda c, d, s: _done(bad))
    with pytest.raises(cli_llm.AgentLLMError, match="rate limited"):
        cli_llm.AgentLLM("opencode").complete("s", "u")


def test_failure_is_reported(monkeypatch):
    monkeypatch.setattr(cli_llm, "_resolve", lambda k: ["claude"])
    monkeypatch.setattr(cli_llm, "_run", lambda c, d, s: _done("", "boom", 1))
    with pytest.raises(cli_llm.AgentLLMError, match="boom"):
        cli_llm.AgentLLM("claude").complete("s", "u")


def test_hook_ignores_nested_calls(monkeypatch, capsys):
    monkeypatch.setenv("CODEC_NESTED", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "/compacter carry", "cwd": "."})))
    assert hook.run("claude-code") == 0 and capsys.readouterr().out == ""

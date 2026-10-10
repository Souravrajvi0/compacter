"""Per-agent-version compatibility: every fixture in tests/data/compat/<agent>/<version>.* must read,
keep tool pairs together, and survive a write -> read round trip. New agent release = new fixture."""
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import ctxpipe.adapters.claude_code as claude_code
import ctxpipe.adapters.codex as codex
import ctxpipe.adapters.opencode as opencode
from ctxpipe import compat
from ctxpipe.adapters import get_adapter

ROOT = compat.FIXTURES


def _cases():
    """One case per fixture file: <version>.* is hand-built, <version>-real.* is a real captured session."""
    return [pytest.param(a, p.name, id=f"{a}-{p.stem}")
            for a in compat.CLI if (ROOT / a).is_dir() for p in sorted((ROOT / a).iterdir())]


def _install(agent: str, fixture: str, tmp: Path, monkeypatch) -> None:
    text = (ROOT / agent / fixture).read_text(encoding="utf-8")
    if agent == "claude-code":
        d = tmp / "home" / claude_code.encode_project_dir(tmp)
        d.mkdir(parents=True)
        (d / "s.jsonl").write_text(text, encoding="utf-8")
        monkeypatch.setattr(claude_code, "CLAUDE_HOME", tmp / "home")
    elif agent == "codex":
        d = tmp / "home" / "2026" / "10" / "01"
        d.mkdir(parents=True)
        (d / "rollout-x.jsonl").write_text(text.replace("@PROJECT@", json.dumps(str(tmp))[1:-1]),
                                           encoding="utf-8")
        monkeypatch.setattr(codex, "CODEX_HOME", tmp / "home")
    else:
        db = tmp / "opencode.db"
        con = sqlite3.connect(db)
        con.executescript(text.replace("@PROJECT@", str(tmp.resolve()).replace("'", "''")))
        con.commit()
        con.close()
        monkeypatch.setattr(opencode, "DB_FILE", db)


@pytest.mark.parametrize("agent,fixture", _cases())
def test_reads_fixture(agent, fixture, tmp_path, monkeypatch):
    _install(agent, fixture, tmp_path, monkeypatch)
    s = get_adapter(agent).read(tmp_path)
    assert s.messages, "fixture produced no messages"
    assert {m.role for m in s.messages} <= {"user", "assistant", "tool"}
    assert all(m.content.strip() for m in s.messages)
    assert s.messages[0].role == "user"
    assert [i for m in s.messages for i in m.pair_ids()], "tool call ids were lost"


@pytest.mark.parametrize("agent,fixture", _cases())
def test_write_then_read_roundtrip(agent, fixture, tmp_path, monkeypatch):
    _install(agent, fixture, tmp_path, monkeypatch)
    ad = get_adapter(agent)
    s = ad.read(tmp_path)
    texts = [m.content for m in s.messages if m.role != "tool"]
    src = Path(s.meta["source_file"]) if agent != "opencode" else None
    before = src.read_bytes() if src else None
    ad.write(s, tmp_path)
    if src:
        assert src.read_bytes() == before, "original session must not be modified"
        newest = max((tmp_path / "home").rglob("*.jsonl"), key=lambda p: p.stat().st_mtime_ns)
        assert newest != src
        os.utime(newest, (time.time() + 5, time.time() + 5))
    else:
        monkeypatch.setenv("CTXPIPE_OPENCODE_SESSION", s.meta["new_session_id"])
    again = ad.read(tmp_path)
    assert [m.content for m in again.messages if m.role != "tool"] == texts


def test_verified_list_matches_fixtures():
    for agent in compat.CLI:
        assert compat.tested_versions(agent) == compat.fixture_versions(agent), agent


def test_every_agent_has_a_verified_version():
    for agent in compat.CLI:
        assert compat.tested_versions(agent), f"no fixture for {agent}"


@pytest.mark.parametrize("installed,tested,status", [
    (None, ["1.0.0"], "missing"), ("1.0.0", [], "untested"), ("1.0.0", ["1.0.0", "1.2.0"], "ok"),
    ("1.3.0", ["1.0.0", "1.2.0"], "newer"), ("0.9.0", ["1.0.0", "1.2.0"], "older"),
    ("1.1.0", ["1.0.0", "1.2.0"], "untested"),
])
def test_classify(installed, tested, status):
    assert compat.classify(installed, tested) == status


@pytest.mark.parametrize("agent", list(compat.CLI))
def test_installed_agent_is_verified(agent):
    """Live check: xfails (does not fail the suite) when the CLI on this machine is not a verified version."""
    r = compat.check(agent)
    if r.status == "missing":
        pytest.skip(f"{agent} not installed")
    if r.status != "ok":
        pytest.xfail(r.line())

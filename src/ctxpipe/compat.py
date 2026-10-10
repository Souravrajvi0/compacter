"""Which agent versions ctxpipe has been verified against, and a check for the one you have installed.

The list lives in verified_versions.json (shipped in the wheel). Each entry must have a fixture under
tests/data/compat/<agent>/<version>.*; tests/test_compat.py enforces that the two stay in sync.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

CLI = {"claude-code": "claude", "codex": "codex", "opencode": "opencode"}
VERIFIED_FILE = Path(__file__).with_name("verified_versions.json")
FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "data" / "compat"  # repo checkouts only
_VERSION = re.compile(r"\d+\.\d+\.\d+")

_NOTES = {
    "ok": "verified",
    "newer": "newer than any tested version; run `pytest tests/test_compat.py` and re-check",
    "older": "older than the oldest tested version; may not work",
    "untested": "not one of the verified versions",
    "missing": "not installed",
}


@dataclass
class Report:
    agent: str
    installed: str | None
    tested: list[str]
    status: str  # ok | newer | older | untested | missing

    def line(self) -> str:
        tested = ", ".join(self.tested) or "none"
        return f"{self.agent:<12} installed={self.installed or '-':<10} tested=[{tested}]  {_NOTES[self.status]}"


def _key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def tested_versions(agent: str) -> list[str]:
    data = json.loads(VERIFIED_FILE.read_text(encoding="utf-8"))
    return sorted(data.get(agent, []), key=_key)


def fixture_versions(agent: str, root: Path = FIXTURES) -> list[str]:
    """Versions that have a fixture on disk (repo checkout only)."""
    d = root / agent
    if not d.is_dir():
        return []
    return sorted({m.group(0) for p in d.iterdir() if (m := _VERSION.match(p.name))}, key=_key)


def installed_version(agent: str) -> str | None:
    exe = shutil.which(CLI[agent])
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30,
                             shell=exe.lower().endswith((".cmd", ".bat"))).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = _VERSION.search(out)
    return m.group(0) if m else None


def classify(installed: str | None, tested: list[str]) -> str:
    if installed is None:
        return "missing"
    if not tested:
        return "untested"
    if installed in tested:
        return "ok"
    if _key(installed) > _key(tested[-1]):
        return "newer"
    return "older" if _key(installed) < _key(tested[0]) else "untested"


def check(agent: str) -> Report:
    inst, tested = installed_version(agent), tested_versions(agent)
    return Report(agent, inst, tested, classify(inst, tested))


def check_all() -> list[Report]:
    return [check(a) for a in CLI]

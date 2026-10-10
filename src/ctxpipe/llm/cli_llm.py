"""Summarize with the coding agent you already use; no separate API key needed.

Each agent's non-interactive mode is driven as a subprocess, reusing its existing login:
  claude   -> `claude -p`      codex -> `codex exec`      opencode -> `opencode run`
Runs happen in a throwaway directory with persistence off where the CLI allows it, so the helper call
never shows up as your project's "latest session".
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

NESTED_ENV = "CTXPIPE_NESTED"  # set for the child so our own hooks ignore the helper call
TIMEOUT = 300
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class AgentLLMError(RuntimeError):
    pass


def available(kind: str) -> bool:
    return shutil.which(kind) is not None


def _resolve(kind: str) -> list[str]:
    exe = shutil.which(kind)
    if not exe:
        raise AgentLLMError(f"`{kind}` not found on PATH; install it or set ANTHROPIC_API_KEY and use --llm anthropic")
    return [exe]


def _run(cmd: list[str], cwd: str, stdin: str | None) -> subprocess.CompletedProcess:
    shell = cmd[0].lower().endswith((".cmd", ".bat"))
    try:
        return subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd,
            timeout=TIMEOUT, shell=shell, env={**os.environ, NESTED_ENV: "1"},
        )
    except subprocess.TimeoutExpired as e:
        raise AgentLLMError(f"{cmd[0]} timed out after {TIMEOUT}s") from e


def _opencode_text(stdout: str) -> str:
    """Text parts from `opencode run --format json`; raises with the upstream message on error events."""
    texts: list[str] = []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "error":
            err = ev.get("error", {})
            raise AgentLLMError(f"opencode: {(err.get('data') or {}).get('message') or err.get('name') or 'error'}")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else ev
        if ev.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
    return "".join(texts).strip()


class AgentLLM:
    def __init__(self, kind: str) -> None:
        if kind not in ("claude", "codex", "opencode"):
            raise ValueError(f"unknown agent LLM {kind!r}")
        self.kind = kind

    def complete(self, system: str, user: str, max_tokens: int = 2048) -> str:
        bin_ = _resolve(self.kind)
        with tempfile.TemporaryDirectory(prefix="ctxpipe-llm-") as tmp:
            if self.kind == "claude":
                cmd = [*bin_, "-p", "--no-session-persistence", "--tools", "", "--max-turns", "1",
                       "--system-prompt", system]
                r = _run(cmd, tmp, user)
                out = r.stdout
            elif self.kind == "codex":
                last = Path(tmp) / "answer.txt"
                cmd = [*bin_, "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only",
                       "-o", str(last), "-"]
                r = _run(cmd, tmp, f"{system}\n\n{user}")
                out = last.read_text(encoding="utf-8") if last.exists() else r.stdout
            else:
                prompt = Path(tmp) / "ctxpipe-prompt.md"
                prompt.write_text(f"{system}\n\n{user}", encoding="utf-8")
                cmd = [*bin_, "run", "--dir", tmp, "--format", "json", "-f", str(prompt),
                       "Follow the instructions in the attached file exactly. Output only the requested result."]
                r = _run(cmd, tmp, None)
                out = _opencode_text(r.stdout) or r.stdout
            text = _ANSI.sub("", out).strip()
            if r.returncode != 0 or not text:
                detail = (r.stderr or r.stdout or "no output").strip().splitlines()[-1:]
                raise AgentLLMError(f"{self.kind} summarizer failed: {detail[0] if detail else 'no output'}")
            return text

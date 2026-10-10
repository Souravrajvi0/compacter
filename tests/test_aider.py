from pathlib import Path

from codec.adapters import get_adapter
from codec.core.models import Message, Session

SAMPLE = """# aider chat started at 2026-10-06 10:00:00

#### user

fix the bug

#### assistant

done, patched main.py

#### user

thanks
"""


def test_aider_read(tmp_path: Path):
    (tmp_path / ".aider.chat.history.md").write_text(SAMPLE, encoding="utf-8")
    s = get_adapter("aider").read(tmp_path)
    assert [m.role for m in s.messages] == ["user", "assistant", "user"]
    assert s.messages[0].content == "fix the bug"
    assert s.messages[1].content == "done, patched main.py"


def test_aider_roundtrip_write_does_not_overwrite(tmp_path: Path):
    src = tmp_path / ".aider.chat.history.md"
    src.write_text(SAMPLE, encoding="utf-8")
    s = Session(agent="aider", project=str(tmp_path))
    s.messages = [Message("user", "hi"), Message("assistant", "hello"), Message("tool", "ignored")]
    out = get_adapter("aider").write(s, tmp_path)
    assert out.name == ".aider.chat.history.compacted.md"
    assert src.read_text(encoding="utf-8") == SAMPLE
    text = out.read_text(encoding="utf-8")
    assert "#### user" in text and "ignored" not in text

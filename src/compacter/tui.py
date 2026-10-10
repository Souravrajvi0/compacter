"""Terminal checklist for choosing which messages go into compaction.

`Picker` is a pure state machine (keys in, lines out) so it can be tested without a TTY;
`pick_messages` wires it to the real terminal.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Callable, TextIO

from .core.models import Message
from .core.tokens import estimate_tokens

CHECK, EMPTY = "✓", " "
UP, DOWN, SPACE, ENTER, ESC = "up", "down", "space", "enter", "esc"


def parse_ranges(spec: str, size: int) -> set[int]:
    """'0,3-5,-1' -> {0,3,4,5,size-1}. Negative indexes count from the end."""
    out: set[int] = set()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        lo, sep, hi = part.partition("-") if not part.startswith("-") else (part, "", "")
        a, b = (int(lo), int(hi)) if sep else (int(part), int(part))
        a, b = (a + size if a < 0 else a), (b + size if b < 0 else b)
        if not (0 <= a <= b < size):
            raise ValueError(f"range {part!r} out of bounds for {size} messages")
        out.update(range(a, b + 1))
    return out


class Picker:
    def __init__(self, messages: list[Message], indexes: list[int], selected: set[str],
                 height: int = 20, width: int = 100):
        self.messages, self.indexes = messages, indexes   # indexes: position in the session
        self.selected = set(selected)
        self.cursor, self.top = 0, 0
        self.height, self.width = max(5, height), max(40, width)

    # ---- state -------------------------------------------------------------
    def _partners(self, row: int) -> list[int]:
        """Rows holding the same tool_use/tool_result pair; they toggle together."""
        ids = self.messages[row].pair_ids()
        return [r for r, m in enumerate(self.messages) if r == row or (ids and ids & m.pair_ids())]

    def toggle(self) -> None:
        rows = self._partners(self.cursor)
        on = self.messages[self.cursor].id not in self.selected
        for r in rows:
            (self.selected.add if on else self.selected.discard)(self.messages[r].id)

    def handle(self, key: str) -> str | None:
        """Returns 'confirm' / 'cancel' when finished, else None."""
        n = len(self.messages)
        if key in (UP, "k"):
            self.cursor = max(0, self.cursor - 1)
        elif key in (DOWN, "j"):
            self.cursor = min(n - 1, self.cursor + 1)
        elif key == SPACE:
            self.toggle()
        elif key == "a":
            self.selected = {m.id for m in self.messages}
        elif key == "n":
            self.selected = set()
        elif key == "i":
            self.selected = {m.id for m in self.messages} - self.selected
        elif key == ENTER:
            return "confirm"
        elif key in (ESC, "q"):
            return "cancel"
        body = self.height - 3
        self.top = min(max(self.top, self.cursor - body + 1), self.cursor)
        return None

    # ---- view --------------------------------------------------------------
    def render(self) -> list[str]:
        body = self.height - 3
        w = self.width
        sel_tokens = sum(estimate_tokens(m.content) for m in self.messages if m.id in self.selected)
        lines = [f" Pick messages to COMPACT   ({len(self.selected)}/{len(self.messages)} checked,"
                 f" ~{sel_tokens} tokens)"]
        for row in range(self.top, min(self.top + body, len(self.messages))):
            m = self.messages[row]
            mark = CHECK if m.id in self.selected else EMPTY
            left = f"{'>' if row == self.cursor else ' '} {self.indexes[row]:>3} {m.role:<9} "
            room = w - len(left) - 6
            text = " ".join(m.content.split())
            text = (text[: room - 1] + "…") if len(text) > room else text
            line = f"{left}{text:<{room}} [{mark}]"
            lines.append(f"\x1b[7m{line}\x1b[0m" if row == self.cursor else line)
        lines += [""] * (body - (len(lines) - 1))
        lines.append(" ↑/↓ move  space toggle  a all  n none  i invert  enter confirm  q cancel")
        return lines


# ---- terminal plumbing ---------------------------------------------------------
def read_key() -> str:
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": UP, "P": DOWN}.get(msvcrt.getwch(), "")
        if ch == "\x03":
            raise KeyboardInterrupt
    else:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    seq = sys.stdin.read(2)
                    return {"[A": UP, "[B": DOWN}.get(seq, "")
                return ESC
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if ch == "\x03":
            raise KeyboardInterrupt
    return {" ": SPACE, "\r": ENTER, "\n": ENTER, "\x1b": ESC}.get(ch, ch)


def pick_messages(messages: list[Message], indexes: list[int], preselected: set[str],
                  read: Callable[[], str] = read_key, out: TextIO | None = None) -> set[str] | None:
    """Show the checklist; returns the checked message ids, or None if cancelled."""
    out = out or sys.stdout
    if os.name == "nt":
        os.system("")  # enable ANSI escape processing on Windows 10+
    size = shutil.get_terminal_size((100, 24))
    pk = Picker(messages, indexes, preselected, height=size.lines - 1, width=size.columns)
    out.write("\x1b[?25l")                     # hide cursor
    try:
        while True:
            out.write("\x1b[H\x1b[2J" + "\n".join(pk.render()))
            out.flush()
            if (done := pk.handle(read())):
                break
    finally:
        out.write("\x1b[?25h\x1b[H\x1b[2J")   # restore cursor, clear
        out.flush()
    return pk.selected if done == "confirm" else None

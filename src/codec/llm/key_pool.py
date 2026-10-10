"""Round-robin API key pool with per-key cooldown on failure.

Keys are read from the environment, never from files:
  CODEC_ANTHROPIC_KEYS=k1,k2,k3          (comma separated), or
  ANTHROPIC_API_KEY, ANTHROPIC_API_KEY_2, ANTHROPIC_API_KEY_3, ...
"""
from __future__ import annotations

import os
import time
from typing import Callable, TypeVar

T = TypeVar("T")


class AllKeysUnavailable(RuntimeError):
    pass


def keys_from_env(provider: str = "ANTHROPIC") -> list[str]:
    multi = os.environ.get(f"CODEC_{provider}_KEYS", "")
    keys = [k.strip() for k in multi.split(",") if k.strip()]
    if not keys:
        base = f"{provider}_API_KEY"
        names = [base] + [f"{base}_{i}" for i in range(2, 21)]
        keys = [os.environ[n] for n in names if os.environ.get(n)]
    return list(dict.fromkeys(keys))  # de-dupe, keep order


class KeyPool:
    def __init__(self, keys: list[str], clock: Callable[[], float] = time.monotonic) -> None:
        if not keys:
            raise ValueError("KeyPool needs at least one key")
        self._keys = list(keys)
        self._cooldown_until: dict[str, float] = {}
        self._i = 0
        self._clock = clock

    def __len__(self) -> int:
        return len(self._keys)

    def next_key(self) -> str:
        now = self._clock()
        for _ in range(len(self._keys)):
            key = self._keys[self._i % len(self._keys)]
            self._i += 1
            if self._cooldown_until.get(key, 0) <= now:
                return key
        raise AllKeysUnavailable("all API keys are cooling down; retry later")

    def cool_down(self, key: str, seconds: float) -> None:
        self._cooldown_until[key] = self._clock() + seconds

    def call(
        self,
        fn: Callable[[str], T],
        classify: Callable[[Exception], float | None],
    ) -> T:
        """Run fn(key), rotating keys on failure.

        classify(exc) returns a cooldown in seconds if the error is key-specific
        (rate limit, bad key, overloaded), or None if it should be raised as-is.
        """
        last: Exception | None = None
        for _ in range(len(self._keys)):
            try:
                key = self.next_key()
            except AllKeysUnavailable:
                break
            try:
                return fn(key)
            except Exception as e:  # noqa: BLE001
                cooldown = classify(e)
                if cooldown is None:
                    raise
                self.cool_down(key, cooldown)
                last = e
        raise AllKeysUnavailable("all API keys failed or are cooling down") from last

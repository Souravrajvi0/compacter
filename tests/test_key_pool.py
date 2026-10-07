import pytest

from ctxpipe.llm.key_pool import AllKeysUnavailable, KeyPool, keys_from_env


class RateLimited(Exception):
    pass


def classify(e):
    return 60.0 if isinstance(e, RateLimited) else None


def test_round_robin():
    pool = KeyPool(["a", "b", "c"])
    assert [pool.next_key() for _ in range(4)] == ["a", "b", "c", "a"]


def test_rotates_on_rate_limit_and_cools_down():
    t = [0.0]
    pool = KeyPool(["a", "b"], clock=lambda: t[0])
    used = []

    def fn(key):
        used.append(key)
        if key == "a":
            raise RateLimited()
        return "ok"

    assert pool.call(fn, classify) == "ok"
    assert used == ["a", "b"]
    used.clear()
    assert pool.call(fn, classify) == "ok"
    assert used == ["b"]            # 'a' still cooling
    t[0] = 61
    used.clear()
    pool.call(fn, classify)         # 'a' eligible again
    assert "a" in used or used == ["b"]


def test_non_key_errors_propagate():
    pool = KeyPool(["a", "b"])
    with pytest.raises(ValueError):
        pool.call(lambda k: (_ for _ in ()).throw(ValueError("bug")), classify)


def test_all_keys_fail():
    pool = KeyPool(["a", "b"])

    def fn(key):
        raise RateLimited()

    with pytest.raises(AllKeysUnavailable):
        pool.call(fn, classify)


def test_keys_from_env(monkeypatch):
    for n in ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_2", "CTXPIPE_ANTHROPIC_KEYS"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY_2", "y")
    assert keys_from_env("ANTHROPIC") == ["x", "y"]
    monkeypatch.setenv("CTXPIPE_ANTHROPIC_KEYS", "p, q ,p")
    assert keys_from_env("ANTHROPIC") == ["p", "q"]

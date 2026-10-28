"""Tests for robot/bot/active_game.py (per-user current-game tracking)."""

from __future__ import annotations

import asyncio

import pytest

from robot.bot import active_game


class _FakeRedis:
    def __init__(self, *, fail: bool = False):
        self.store: dict[str, str] = {}
        self.fail = fail
        self.last_ex = None

    async def get(self, k):
        if self.fail:
            raise RuntimeError("redis down")
        return self.store.get(k)

    async def set(self, k, v, ex=None):
        if self.fail:
            raise RuntimeError("redis down")
        self.store[k] = v
        self.last_ex = ex

    async def delete(self, k):
        if self.fail:
            raise RuntimeError("redis down")
        self.store.pop(k, None)


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr(active_game, "redis_client", r)
    return r


def test_key_format():
    assert active_game._key("12345") == "bot:active_game:12345"


def test_set_then_get(fake_redis):
    asyncio.run(active_game.set_active("u1", "ai_1_abcdef"))
    assert asyncio.run(active_game.get_active("u1")) == "ai_1_abcdef"
    assert fake_redis.last_ex == active_game._TTL_SECONDS  # TTL applied


def test_get_none_when_absent(fake_redis):
    assert asyncio.run(active_game.get_active("nobody")) is None


def test_set_empty_is_noop(fake_redis):
    asyncio.run(active_game.set_active("u1", ""))
    assert asyncio.run(active_game.get_active("u1")) is None


def test_clear(fake_redis):
    asyncio.run(active_game.set_active("u1", "ai_1_abcdef"))
    asyncio.run(active_game.clear_active("u1"))
    assert asyncio.run(active_game.get_active("u1")) is None


def test_redis_failure_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(active_game, "redis_client", _FakeRedis(fail=True))
    # must not raise
    assert asyncio.run(active_game.get_active("u1")) is None
    asyncio.run(active_game.set_active("u1", "ai_1_abcdef"))
    asyncio.run(active_game.clear_active("u1"))

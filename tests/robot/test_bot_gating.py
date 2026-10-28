"""Tests for the bot creation gate (⑤): kill-switch / content / quota."""

from __future__ import annotations

import asyncio

import robot.config as cfg
from robot.bot import gating, service


# ── screen_prompt (content guard) ──

def test_screen_empty():
    assert gating.screen_prompt("   ").reason == "empty"


def test_screen_too_long():
    assert gating.screen_prompt("x" * 3000).reason == "too_long"


def test_screen_banned_en():
    assert gating.screen_prompt("please make a rape game").reason == "content_blocked"


def test_screen_banned_zh():
    assert gating.screen_prompt("做个强奸游戏").reason == "content_blocked"


def test_screen_ok():
    r = gating.screen_prompt("a top-down space shooter")
    assert r.allowed and r.reason == ""


# ── gate (full pipeline) ──

def test_gate_killswitch(monkeypatch):
    monkeypatch.setattr(cfg.settings, "bot_creation_enabled", False, raising=False)
    r = asyncio.run(gating.gate("a game", requester_key="u1", action="create"))
    assert not r.allowed and r.reason == "disabled"


def test_gate_content_blocked(monkeypatch):
    monkeypatch.setattr(cfg.settings, "bot_creation_enabled", True, raising=False)
    r = asyncio.run(gating.gate("强奸 game", requester_key="u1", action="create"))
    assert r.reason == "content_blocked"


def test_gate_quota_exceeded(monkeypatch):
    monkeypatch.setattr(cfg.settings, "bot_creation_enabled", True, raising=False)

    async def deny(*a, **k):
        return False

    monkeypatch.setattr(gating, "check_ws_create_limit", deny)
    r = asyncio.run(gating.gate("a game", requester_key="u1", action="create"))
    assert not r.allowed and r.reason == "quota_exceeded"


def test_gate_allows(monkeypatch):
    monkeypatch.setattr(cfg.settings, "bot_creation_enabled", True, raising=False)

    async def allow(*a, **k):
        return True

    monkeypatch.setattr(gating, "check_ws_create_limit", allow)
    r = asyncio.run(gating.gate("a game", requester_key="u1", action="create"))
    assert r.allowed


def test_gate_quota_uses_modify_limit(monkeypatch):
    monkeypatch.setattr(cfg.settings, "bot_creation_enabled", True, raising=False)
    seen = {}

    async def capture(user_id, action, limit, window):
        seen["action"] = action
        seen["limit"] = limit
        return True

    monkeypatch.setattr(gating, "check_ws_create_limit", capture)
    monkeypatch.setattr(cfg.settings, "bot_daily_modify_limit", 7, raising=False)
    asyncio.run(gating.gate("tweak it", requester_key="u1", action="modify"))
    assert seen["action"] == "bot_game_modify" and seen["limit"] == 7


# ── format_blocked ──

def test_format_blocked_all_reasons():
    for reason in ("disabled", "empty", "too_long", "content_blocked", "quota_exceeded"):
        assert service.format_blocked(reason, zh=True)
        assert service.format_blocked(reason, zh=False)


def test_format_blocked_unknown_reason_has_fallback():
    assert service.format_blocked("weird", zh=True)
    assert service.format_blocked("weird", zh=False)

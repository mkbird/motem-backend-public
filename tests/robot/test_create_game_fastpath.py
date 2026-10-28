"""Tests for create_game's centralized internal fast-path gating.

Fast-path logic now lives in game_tools._try_fastpath and create_game tries it
first. We verify the GATING (when it's called vs skipped) without running the
heavy slow path: a fast hit returns early; when skipped, create_game falls
through and we short-circuit it via a missing TEMPLATE_DIR so no SDK runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import robot.config as cfg
from robot.agent.tools import game_tools
from robot.agent.tools import set_request_context
from robot.agent.tools.game_tools import create_game


@pytest.fixture(autouse=True)
def _ctx():
    set_request_context("owner-1", "user-1", "admin", "", "zh-Hans", "")
    # ensure no leftover skip flag between tests
    tok = game_tools._skip_fastpath.set(False)
    yield
    game_tools._skip_fastpath.reset(tok)


def _call(**kw):
    args = {"title": "t", "description": "a catch-the-bananas game", "input_mode": "body_pose"}
    args.update(kw)
    return asyncio.run(create_game.ainvoke(args))


def test_fast_hit_returns_early(monkeypatch):
    monkeypatch.setattr(cfg.settings, "fastpath_enabled", True, raising=False)
    fp = AsyncMock(return_value="游戏「接香蕉」创建完成！游戏ID: ai_1_abcdef")
    monkeypatch.setattr(game_tools, "_try_fastpath", fp)
    out = _call()
    assert "ai_1_abcdef" in out
    fp.assert_awaited_once()


def test_flag_off_skips_fastpath(monkeypatch):
    monkeypatch.setattr(cfg.settings, "fastpath_enabled", False, raising=False)
    fp = AsyncMock(side_effect=AssertionError("must not run when flag off"))
    monkeypatch.setattr(game_tools, "_try_fastpath", fp)
    monkeypatch.setattr(game_tools, "TEMPLATE_DIR", Path("/no/such/template/dir"))
    out = _call()  # falls through, short-circuits on missing template
    fp.assert_not_called()
    assert isinstance(out, str) and out


def test_template_id_skips_fastpath(monkeypatch):
    monkeypatch.setattr(cfg.settings, "fastpath_enabled", True, raising=False)
    fp = AsyncMock(side_effect=AssertionError("must not run when template given"))
    monkeypatch.setattr(game_tools, "_try_fastpath", fp)
    monkeypatch.setattr(game_tools, "TEMPLATE_DIR", Path("/no/such/template/dir"))
    _call(template_game_id="ai_1234567890_abcdef")
    fp.assert_not_called()


def test_mock_mode_skips_fastpath(monkeypatch):
    monkeypatch.setattr(cfg.settings, "fastpath_enabled", True, raising=False)
    monkeypatch.setattr(game_tools, "_is_mock_creation_mode", lambda: True)
    fp = AsyncMock(side_effect=AssertionError("must not run in mock mode"))
    monkeypatch.setattr(game_tools, "_try_fastpath", fp)
    monkeypatch.setattr(game_tools, "TEMPLATE_DIR", Path("/no/such/template/dir"))
    _call()
    fp.assert_not_called()


def test_skip_flag_skips_fastpath(monkeypatch):
    monkeypatch.setattr(cfg.settings, "fastpath_enabled", True, raising=False)
    fp = AsyncMock(side_effect=AssertionError("must not run when skip flag set"))
    monkeypatch.setattr(game_tools, "_try_fastpath", fp)
    monkeypatch.setattr(game_tools, "TEMPLATE_DIR", Path("/no/such/template/dir"))
    game_tools._skip_fastpath.set(True)
    _call()
    fp.assert_not_called()

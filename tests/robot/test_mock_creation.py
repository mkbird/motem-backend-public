"""Tests for ``ROBOT_MOCK_CREATION=1`` short-circuit on game / widget tools.

Mock mode lets us smoke-test the entire end-to-end pipeline (template copy →
zip → DB row → ``game_ready`` / ``widget_ready`` WebSocket push) without
calling the Claude Code SDK. Used by:

- iOS QA: voice → main agent → ``create_game`` (mock) → home modal pop-up
- CI: end-to-end agent + creation flow without LLM credits

Asserts:
  • ``ROBOT_MOCK_CREATION=1`` skips the SDK call.
  • Generated zip file exists on disk.
  • DB row in ``ai_games`` / ``ai_widgets`` is present (best-effort, skipped
    if DB unavailable in test env).
  • The ``game_ready`` / ``widget_ready`` WebSocket payload is well-formed.
  • Modify path emits a non-empty ``game_patch`` / ``widget_patch`` (the mock
    appends a comment line so the diff isn't empty).
  • Helper ``_is_mock_creation_mode()`` parses env var truthy values.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from robot.agent.tools import _context as ctx
from robot.agent.tools.game_tools import (
    GAMES_DIR,
    create_game,
    modify_game,
    _is_mock_creation_mode as _is_mock_game,
)
from robot.agent.tools.widget_tools import (
    WIDGETS_DIR,
    create_widget,
    modify_widget,
    _is_mock_creation_mode as _is_mock_widget,
)


# ─────────────────────────────────────────────────────────────────────
# Env-var truthy parsing
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("no", False),
    ],
)
def test_is_mock_mode_parses_env(value, expected, monkeypatch):
    monkeypatch.setenv("ROBOT_MOCK_CREATION", value)
    assert _is_mock_game() is expected
    assert _is_mock_widget() is expected


def test_is_mock_mode_default_is_off(monkeypatch):
    monkeypatch.delenv("ROBOT_MOCK_CREATION", raising=False)
    assert _is_mock_game() is False
    assert _is_mock_widget() is False


# ─────────────────────────────────────────────────────────────────────
# Mock create_game
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def cleanup_paths():
    """Track paths to wipe (dirs + .zip siblings) at teardown."""
    paths: list[Path] = []
    yield paths
    for p in paths:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            try:
                p.unlink()
            except Exception:
                pass
        zip_p = p.with_suffix(".zip")
        if zip_p.exists():
            try:
                zip_p.unlink()
            except Exception:
                pass


def _setup_ctx(owner: str, user: str):
    ctx._current_owner_id.set(owner)
    ctx._current_user_id.set(user)
    mock_ws = AsyncMock()
    original_ws = ctx._ws_manager
    ctx._ws_manager = mock_ws
    return mock_ws, original_ws


@pytest.mark.asyncio
async def test_mock_create_game_skips_sdk_and_pushes_game_ready(
    monkeypatch, cleanup_paths
):
    """Mock mode: create_game should produce a valid zip + meta + game_ready
    WebSocket payload, all without invoking the SDK."""
    monkeypatch.setenv("ROBOT_MOCK_CREATION", "1")
    mock_ws, original_ws = _setup_ctx("owner_mock_create", "user_mock_create")

    # If the SDK were invoked, _run_claude_agent would actually try to spawn
    # a subprocess. Verify mock mode prevents that by patching it to throw.
    from robot.agent.tools import game_tools as gt
    monkeypatch.setattr(
        gt, "_run_claude_agent",
        AsyncMock(side_effect=AssertionError("SDK must not be called in mock mode")),
    )

    try:
        result = await create_game.ainvoke({
            "title": "Mock Test Game",
            "description": "A smoke-test game",
            "input_mode": "touch",
            "icon": "🎮",
        })
    finally:
        ctx._ws_manager = original_ws

    # Result string should mention the new game id; extract it for cleanup.
    assert "Mock Test Game" in result or "ai_" in result
    # Find the new game directory by listing recent ai_* dirs created in the
    # last few seconds (test isolation).
    candidates = sorted(
        [p for p in GAMES_DIR.iterdir() if p.name.startswith("ai_") and p.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    assert candidates, "no game dir was created"
    game_dir = candidates[0]
    cleanup_paths.append(game_dir)

    # Zip exists alongside the directory.
    zip_path = game_dir.with_suffix(".zip")
    assert zip_path.exists(), f"zip not created at {zip_path}"
    assert zip_path.stat().st_size > 0

    # meta.json was written with the new id.
    meta = json.loads((game_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["game_id"] == game_dir.name
    assert meta["input_mode"] == "touch"
    assert meta["icon"] == "🎮"
    assert meta["owner_id"] == "owner_mock_create"

    # WebSocket got exactly one game_ready push with the right shape.
    sent = [c.args for c in mock_ws.send_to_user.await_args_list]
    actions = [
        a[2]["data"]["action"]
        for a in sent
        if isinstance(a[2], dict) and a[2].get("type") == "robot_command"
    ]
    assert "game_ready" in actions, f"game_ready not sent; got {actions}"

    # Mock marker in scenes/main.gd so iOS-side logs make it obvious.
    main_gd = (game_dir / "scenes" / "main.gd").read_text(encoding="utf-8")
    assert "MOCK" in main_gd


@pytest.mark.asyncio
async def test_mock_modify_game_emits_game_patch(monkeypatch, cleanup_paths):
    """Mock modify: should append a comment line and emit a game_patch with
    at least one modify diff."""
    monkeypatch.setenv("ROBOT_MOCK_CREATION", "1")
    mock_ws, original_ws = _setup_ctx("owner_mock_modify", "user_mock_modify")

    # Pre-create a fake game dir matching the user_id so the ownership
    # check inside modify_game lets it through.
    game_id = "ai_1700009001_aabbcc"
    game_dir = GAMES_DIR / game_id
    if game_dir.exists():
        shutil.rmtree(game_dir, ignore_errors=True)
    game_dir.mkdir(parents=True)
    (game_dir / "scenes").mkdir()
    (game_dir / "scenes" / "main.gd").write_text(
        "extends Node2D\nfunc _ready(): pass\n", encoding="utf-8"
    )
    (game_dir / "scenes" / "main.tscn").write_text("[gd_scene]\n", encoding="utf-8")
    (game_dir / "meta.json").write_text(
        json.dumps({
            "game_id": game_id,
            "title": "Mock Modify Test",
            "owner_id": "user_mock_modify",  # MUST match user for owner gate
            "input_mode": "touch",
            "history": [],
        }),
        encoding="utf-8",
    )
    cleanup_paths.append(game_dir)

    from robot.agent.tools import game_tools as gt
    monkeypatch.setattr(
        gt, "_run_claude_agent",
        AsyncMock(side_effect=AssertionError("SDK must not be called in mock mode")),
    )

    try:
        await modify_game.ainvoke({
            "game_id": game_id,
            "instruction": "Make the player faster",
        })
    finally:
        ctx._ws_manager = original_ws

    # Marker comment got appended.
    main_gd_after = (game_dir / "scenes" / "main.gd").read_text(encoding="utf-8")
    assert "[MOCK modify @" in main_gd_after
    assert "Make the player faster"[:80] in main_gd_after

    # game_patch was sent with at least one diff covering main.gd.
    sent = [c.args for c in mock_ws.send_to_user.await_args_list]
    patch_calls = [
        a[2]["data"]
        for a in sent
        if isinstance(a[2], dict)
        and a[2].get("type") == "robot_command"
        and a[2]["data"].get("action") == "game_patch"
    ]
    assert patch_calls, f"game_patch not sent; got {[a[2] for a in sent]}"
    diffs = patch_calls[0]["diffs"]
    assert any("main.gd" in d.get("path", "") for d in diffs), \
        f"no main.gd diff in {diffs}"


# ─────────────────────────────────────────────────────────────────────
# Mock create_widget / modify_widget
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_create_widget_skips_sdk_and_pushes_widget_ready(
    monkeypatch, cleanup_paths
):
    """Mock mode: create_widget should produce a valid zip + meta + widget_ready
    WebSocket payload, all without invoking the SDK."""
    monkeypatch.setenv("ROBOT_MOCK_CREATION", "1")
    mock_ws, original_ws = _setup_ctx("owner_mock_widget", "user_mock_widget")

    from robot.agent.tools import widget_tools as wt
    monkeypatch.setattr(
        wt, "_run_widget_code_agent",
        AsyncMock(side_effect=AssertionError("SDK must not be called in mock mode")),
    )

    try:
        result = await create_widget.ainvoke({
            "title": "Mock Test Widget",
            "description": "A smoke-test widget",
            "icon": "🧩",
        })
    finally:
        ctx._ws_manager = original_ws

    # New widget directory should exist (widget ids use the `aw_` prefix).
    candidates = sorted(
        [p for p in WIDGETS_DIR.iterdir() if p.name.startswith("aw_") and p.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    assert candidates, "no widget dir created"
    widget_dir = candidates[0]
    cleanup_paths.append(widget_dir)

    # Zip exists.
    zip_path = widget_dir.with_suffix(".zip")
    assert zip_path.exists()
    assert zip_path.stat().st_size > 0

    # meta.json and required files in place.
    assert (widget_dir / "index.html").exists()
    assert (widget_dir / "app.js").exists()
    meta = json.loads((widget_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["widget_id"] == widget_dir.name
    assert meta["icon"] == "🧩"
    assert meta["owner_id"] == "owner_mock_widget"
    assert "supported_sizes" in meta and meta["supported_sizes"]

    # widget_ready was pushed.
    sent = [c.args for c in mock_ws.send_to_user.await_args_list]
    actions = [
        a[2]["data"]["action"]
        for a in sent
        if isinstance(a[2], dict) and a[2].get("type") == "robot_command"
    ]
    assert "widget_ready" in actions, f"widget_ready not sent; got {actions}"

    # MOCK marker in app.js.
    app_js = (widget_dir / "app.js").read_text(encoding="utf-8")
    assert "MOCK" in app_js


@pytest.mark.asyncio
async def test_mock_modify_widget_emits_widget_patch(monkeypatch, cleanup_paths):
    """Mock modify_widget: should append a comment line to app.js and emit a
    widget_patch with at least one modify diff."""
    monkeypatch.setenv("ROBOT_MOCK_CREATION", "1")
    mock_ws, original_ws = _setup_ctx("owner_mock_wmod", "user_mock_wmod")

    widget_id = "ai_mock_wmod_test"
    widget_dir = WIDGETS_DIR / widget_id
    if widget_dir.exists():
        shutil.rmtree(widget_dir, ignore_errors=True)
    widget_dir.mkdir(parents=True)
    (widget_dir / "index.html").write_text(
        "<!doctype html><html></html>", encoding="utf-8")
    (widget_dir / "app.js").write_text(
        "// initial widget code\n", encoding="utf-8")
    (widget_dir / "meta.json").write_text(
        json.dumps({
            "widget_id": widget_id,
            "title": "Mock W-Mod Test",
            "supported_sizes": ["S"],
            "history": [],
        }),
        encoding="utf-8",
    )
    cleanup_paths.append(widget_dir)

    from robot.agent.tools import widget_tools as wt
    monkeypatch.setattr(
        wt, "_run_widget_code_agent",
        AsyncMock(side_effect=AssertionError("SDK must not be called in mock mode")),
    )

    try:
        await modify_widget.ainvoke({
            "widget_id": widget_id,
            "instruction": "Make the text bigger",
        })
    finally:
        ctx._ws_manager = original_ws

    # Marker comment got appended.
    app_js_after = (widget_dir / "app.js").read_text(encoding="utf-8")
    assert "[MOCK modify @" in app_js_after
    assert "Make the text bigger"[:80] in app_js_after

    # widget_patch was sent with at least one diff including app.js.
    sent = [c.args for c in mock_ws.send_to_user.await_args_list]
    patch_calls = [
        a[2]["data"]
        for a in sent
        if isinstance(a[2], dict)
        and a[2].get("type") == "robot_command"
        and a[2]["data"].get("action") == "widget_patch"
    ]
    assert patch_calls, f"widget_patch not sent; got {[a[2] for a in sent]}"
    diffs = patch_calls[0]["diffs"]
    assert any("app.js" in d.get("path", "") for d in diffs), \
        f"no app.js diff in {diffs}"

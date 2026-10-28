"""Tests for create_game / mock cancel + generation-failure checkpoints.

User cancels mid-AI (or WS dropped because app was killed) → Turn.cancelled →
streaming.py 的 cancel_check 回调返 True → create_game 在写 meta/DB 之前 abort
+ 清理 game_dir + zip。**不写 DB**（避免 Mine Tab 草稿污染）。

同样地，AI 生成失败（inactivity timeout 触发的零输出 / main.gd 缺失）也走同一
abort 路径：清 game_dir、不写 DB、给 iOS 推 game_creation_failed —— 避免 iOS
下载到一个空壳 zip 然后白屏。

Coverage:
- create_game 在 cancel_check=True 时不写 DB + 删 game_dir
- mock _mock_create_game 同款行为
- cancel_check=False 时正常完成
- cancel_check=None 时按"未取消"处理（向后兼容）
- create_game 在 AI 返回空 result 时 abort + 推 game_creation_failed
- create_game 在 main.gd 缺失/未被 AI 改写时 abort + 推 game_creation_failed
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from robot.agent.tools import _context as ctx_mod


# ─────────────────────────────────────────────────────────────────────
# create_game cancel checkpoint
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_game_cancel_after_ai_cleans_up(monkeypatch, tmp_path: Path):
    """模拟 AI 完成后立即被取消 → game_dir 删除，DB 不写。"""
    # 隔离 GAMES_DIR 到 tmp_path
    monkeypatch.setattr("robot.agent.tools.game_tools.GAMES_DIR", tmp_path)
    # 真 _template 太重 fake 一份精简的
    fake_template = tmp_path / "_template"
    (fake_template / "scenes").mkdir(parents=True)
    (fake_template / "scripts").mkdir(parents=True)
    (fake_template / "fonts").mkdir(parents=True)
    (fake_template / "scenes" / "main.gd").write_text(
        "extends Node2D\nfunc _ready():\n\tpass\n", encoding="utf-8"
    )
    (fake_template / "scenes" / "main.tscn").write_text(
        '[gd_scene format=3]\n[node name="Root" type="Node2D"]', encoding="utf-8"
    )
    (fake_template / "scripts" / "i18n_strings.gd").write_text(
        "extends Node\nconst STRINGS := {}\n", encoding="utf-8"
    )
    (fake_template / "project.godot").write_text(
        '[application]\nconfig/name="Test"\n[display]\nwindow/size/viewport_width=1194\nwindow/size/viewport_height=834\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("robot.agent.tools.game_tools.TEMPLATE_DIR", fake_template)

    # mock owner_id / user_id
    ctx_mod._current_owner_id.set("test-owner-cancel")
    ctx_mod._current_user_id.set("test-user-cancel")
    ctx_mod._current_user_lang.set("zh-Hans")
    ctx_mod._current_device_model.set("")
    ctx_mod._ws_manager = None  # 不发 game_ready

    # mock _run_claude_agent 返回成功结果，但 cancel_check 一直返 True
    async def _fake_agent(cwd, prompt, input_mode="touch", max_turns=20,
                         use_mcp=True, session_id=""):
        # 写 fake game files 模拟 AI 写完
        (Path(cwd) / "scenes" / "main.gd").write_text(
            "extends Node2D\nfunc _ready():\n\tprint(\"hi\")\n", encoding="utf-8"
        )
        return ("done", "fake-session", 1)

    monkeypatch.setattr("robot.agent.tools.game_tools._run_claude_agent", _fake_agent)

    # mock 验证步骤跳过
    async def _fake_validate(*args, **kwargs):
        return ""
    monkeypatch.setattr("robot.agent.tools.game_tools._validate_and_fix", _fake_validate)

    # cancel_check 永远返 True
    ctx_mod._current_cancel_check.set(lambda: True)

    # 直接调用底层 create_game.func（langchain tool 的 underlying coro）
    from robot.agent.tools.game_tools import create_game
    result = await create_game.ainvoke({
        "title": "X", "description": "test cancel",
        "input_mode": "touch", "icon": "🎮",
        "template_game_id": "",
        "title_i18n": {"en": "X", "zh-Hans": "X"},
        "description_i18n": {"en": "x", "zh-Hans": "x"},
    })

    assert "cancelled" in result.lower()
    # game_dir 已被清掉
    game_dirs = [d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith("ai_")]
    assert game_dirs == [], f"游戏目录未清理: {game_dirs}"
    # zip 不该被生成（cancel 在 zip 之前）
    zips = list(tmp_path.glob("ai_*.zip"))
    assert zips == [], f"zip 不该存在: {zips}"

    # 复位 ContextVar 防污染其他测试
    ctx_mod._current_cancel_check.set(None)


@pytest.mark.asyncio
async def test_create_game_no_cancel_completes_normally(monkeypatch, tmp_path: Path):
    """cancel_check=None 应当继续走完整流程（不影响向后兼容）。"""
    monkeypatch.setattr("robot.agent.tools.game_tools.GAMES_DIR", tmp_path)
    fake_template = tmp_path / "_template"
    (fake_template / "scenes").mkdir(parents=True)
    (fake_template / "scripts").mkdir(parents=True)
    (fake_template / "scenes" / "main.gd").write_text(
        "extends Node2D\n", encoding="utf-8"
    )
    (fake_template / "scenes" / "main.tscn").write_text(
        '[gd_scene format=3]', encoding="utf-8"
    )
    (fake_template / "scripts" / "i18n_strings.gd").write_text("extends Node\n")
    (fake_template / "project.godot").write_text(
        '[application]\n[display]\nwindow/size/viewport_width=1194\nwindow/size/viewport_height=834\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("robot.agent.tools.game_tools.TEMPLATE_DIR", fake_template)

    ctx_mod._current_owner_id.set("test-owner-ok")
    ctx_mod._current_user_id.set("test-user-ok")
    ctx_mod._current_user_lang.set("zh-Hans")
    ctx_mod._current_device_model.set("")
    ctx_mod._current_cancel_check.set(None)
    ctx_mod._ws_manager = None

    async def _fake_agent(cwd, prompt, input_mode="touch", max_turns=20,
                         use_mcp=True, session_id=""):
        # AI 写真实 main.gd 标记（避免 _TEMPLATE_MARKER 触发 incomplete）
        (Path(cwd) / "scenes" / "main.gd").write_text(
            "extends Node2D\nfunc _ready():\n\tprint(\"real\")\n", encoding="utf-8"
        )
        return ("done", "fake-sid", 1)
    monkeypatch.setattr("robot.agent.tools.game_tools._run_claude_agent", _fake_agent)

    async def _fake_validate(*args, **kwargs):
        return ""
    monkeypatch.setattr("robot.agent.tools.game_tools._validate_and_fix", _fake_validate)

    # mock upsert_game_row 计数被调
    upsert_called: list[bool] = []

    async def _fake_upsert(**kwargs):
        upsert_called.append(True)

    import robot.games.service as svc
    monkeypatch.setattr(svc, "upsert_game_row", _fake_upsert)

    from robot.agent.tools.game_tools import create_game
    result = await create_game.ainvoke({
        "title": "Y", "description": "complete normally",
        "input_mode": "touch", "icon": "🎮",
        "template_game_id": "",
        "title_i18n": {"en": "Y", "zh-Hans": "Y"},
        "description_i18n": {"en": "y", "zh-Hans": "y"},
    })

    # 不应是 cancelled 文案
    assert "cancelled" not in result.lower()
    # game_dir 应该存在 + 含 meta.json
    game_dirs = [d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith("ai_")]
    assert len(game_dirs) == 1
    assert (game_dirs[0] / "meta.json").exists()
    # upsert 被调
    assert upsert_called == [True]

    ctx_mod._current_cancel_check.set(None)


# ─────────────────────────────────────────────────────────────────────
# Mock create_game cancel checkpoint
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_create_game_cancel_cleans_up(monkeypatch, tmp_path: Path):
    """Mock 路径在 cancel_check=True 时也应清干净 + 不写 DB。"""
    # 准备一个假 source game
    monkeypatch.setattr("robot.agent.tools.game_tools.GAMES_DIR", tmp_path)
    src = tmp_path / "ai_mock_src"
    (src / "scenes").mkdir(parents=True)
    (src / "scenes" / "main.gd").write_text("extends Node2D\n", encoding="utf-8")
    (src / "scenes" / "main.tscn").write_text("[gd_scene format=3]")
    (src / "meta.json").write_text(json.dumps({"game_id": "ai_mock_src"}))

    ctx_mod._current_owner_id.set("test-owner-mock")
    ctx_mod._current_user_id.set("test-user-mock")
    ctx_mod._current_user_lang.set("zh-Hans")
    ctx_mod._current_device_model.set("")
    ctx_mod._ws_manager = None

    # mock _extract_game_meta 跳过 dashscope 调用
    async def _fake_extract(desc, input_mode="touch"):
        return {"en": "T", "zh-Hans": "T"}, {"en": "D", "zh-Hans": "D"}, "🎮", ""

    import robot.agent.streaming as stream_mod
    monkeypatch.setattr(stream_mod, "_extract_game_meta", _fake_extract)

    # mock canonical assets 跳过 PIL（保持 game_dir 内容简单，确认清理）
    monkeypatch.setattr(
        "robot.agent.tools.game_tools._ensure_canonical_assets",
        lambda d: None,
    )

    upsert_called: list[bool] = []

    async def _fake_upsert(**kwargs):
        upsert_called.append(True)
    import robot.games.service as svc
    monkeypatch.setattr(svc, "upsert_game_row", _fake_upsert)

    # 设 cancel_check=True
    ctx_mod._current_cancel_check.set(lambda: True)

    result = await stream_mod._mock_create_game(
        instruction="anything",
        input_mode="touch",
        mock_source="ai_mock_src",
    )

    assert "cancelled" in result.lower()
    # 新建的 ai_* 目录全清（保留 source 那个 ai_mock_src）
    ai_dirs = sorted(d.name for d in tmp_path.iterdir()
                     if d.is_dir() and d.name.startswith("ai_"))
    assert ai_dirs == ["ai_mock_src"]
    # DB upsert 不应被调
    assert upsert_called == []

    ctx_mod._current_cancel_check.set(None)


# ─────────────────────────────────────────────────────────────────────
# create_game generation-failure cleanup（AI 没产出 / main.gd 缺失）
# ─────────────────────────────────────────────────────────────────────


class _FakeWSManager:
    """记录所有 send_to_user 调用，方便断言 game_creation_failed 被推。"""

    def __init__(self):
        self.sent: list[tuple[str, str, dict]] = []

    async def send_to_user(self, owner_id, user_id, payload):
        self.sent.append((owner_id, user_id, payload))


def _setup_minimal_template(tmp_path: Path) -> None:
    """造一份最小模板，复用三个 failure 测试。"""
    fake_template = tmp_path / "_template"
    (fake_template / "scenes").mkdir(parents=True)
    (fake_template / "scripts").mkdir(parents=True)
    (fake_template / "fonts").mkdir(parents=True)
    # 注意：main.gd 故意带 _TEMPLATE_MARKER 字样 —— 模拟 AI 没改写时的状态。
    (fake_template / "scenes" / "main.gd").write_text(
        "extends Node2D\n# AI will modify this file\nfunc _ready():\n\tpass\n",
        encoding="utf-8",
    )
    (fake_template / "scenes" / "main.tscn").write_text(
        '[gd_scene format=3]\n[node name="Root" type="Node2D"]', encoding="utf-8"
    )
    (fake_template / "scripts" / "i18n_strings.gd").write_text(
        "extends Node\nconst STRINGS := {}\n", encoding="utf-8"
    )
    (fake_template / "project.godot").write_text(
        '[application]\nconfig/name="Test"\n[display]\nwindow/size/viewport_width=1194\nwindow/size/viewport_height=834\n',
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_create_game_empty_result_aborts_and_notifies_ios(monkeypatch, tmp_path: Path):
    """AI 返回空字符串（inactivity timeout 后）→ 不写 DB + 删 game_dir + 推
    game_creation_failed 给 iOS。这是修白屏 bug 的核心断言。"""
    monkeypatch.setattr("robot.agent.tools.game_tools.GAMES_DIR", tmp_path)
    _setup_minimal_template(tmp_path)
    monkeypatch.setattr(
        "robot.agent.tools.game_tools.TEMPLATE_DIR", tmp_path / "_template"
    )

    ctx_mod._current_owner_id.set("test-owner-fail")
    ctx_mod._current_user_id.set("test-user-fail")
    ctx_mod._current_user_lang.set("zh-Hans")
    ctx_mod._current_device_model.set("")
    ctx_mod._current_cancel_check.set(None)
    fake_ws = _FakeWSManager()
    ctx_mod._ws_manager = fake_ws

    # AI 啥都不写、result 空
    async def _fake_agent(cwd, prompt, input_mode="touch", max_turns=20,
                         use_mcp=True, session_id=""):
        # **不写** main.gd —— 模板里那份 _TEMPLATE_MARKER 不会被覆盖
        return ("", "", 0)

    monkeypatch.setattr("robot.agent.tools.game_tools._run_claude_agent", _fake_agent)

    async def _fake_validate(*args, **kwargs):
        return ""
    monkeypatch.setattr("robot.agent.tools.game_tools._validate_and_fix", _fake_validate)

    upsert_called: list[bool] = []

    async def _fake_upsert(**kwargs):
        upsert_called.append(True)

    import robot.games.service as svc
    monkeypatch.setattr(svc, "upsert_game_row", _fake_upsert)

    from robot.agent.tools.game_tools import create_game
    result = await create_game.ainvoke({
        "title": "Z", "description": "fail case",
        "input_mode": "touch", "icon": "🎮",
        "template_game_id": "",
        "title_i18n": {"en": "Z", "zh-Hans": "Z"},
        "description_i18n": {"en": "z", "zh-Hans": "z"},
    })

    # 返回的文案应是失败提示（不是 cancelled / done）
    assert "fail" in result.lower() or "失败" in result, f"unexpected: {result!r}"
    # game_dir 已清
    game_dirs = [d for d in tmp_path.iterdir()
                 if d.is_dir() and d.name.startswith("ai_")]
    assert game_dirs == [], f"game_dir 未清理: {game_dirs}"
    # zip 不该存在
    zips = list(tmp_path.glob("ai_*.zip"))
    assert zips == [], f"zip 不该存在: {zips}"
    # DB upsert 不应被调（避免 Mine Tab 出现空游戏草稿）
    assert upsert_called == []
    # iOS 应收到 game_creation_failed（不是 game_ready）
    actions = [p["data"].get("action") for _, _, p in fake_ws.sent]
    assert "game_creation_failed" in actions, f"actions: {actions}"
    assert "game_ready" not in actions, f"不该推 game_ready: {actions}"
    failed_payload = next(p for _, _, p in fake_ws.sent
                          if p["data"].get("action") == "game_creation_failed")
    assert failed_payload["data"].get("reason") == "ai_no_output"

    ctx_mod._ws_manager = None


@pytest.mark.asyncio
async def test_create_game_missing_main_gd_aborts_and_notifies_ios(monkeypatch, tmp_path: Path):
    """AI 返回了一些 text，但没真正修改 main.gd（仍含 _TEMPLATE_MARKER）→ 同样
    走 fail 分支，避免 iOS 收到 game_ready 后下载到空壳游戏白屏。"""
    monkeypatch.setattr("robot.agent.tools.game_tools.GAMES_DIR", tmp_path)
    _setup_minimal_template(tmp_path)
    monkeypatch.setattr(
        "robot.agent.tools.game_tools.TEMPLATE_DIR", tmp_path / "_template"
    )

    ctx_mod._current_owner_id.set("test-owner-fail2")
    ctx_mod._current_user_id.set("test-user-fail2")
    ctx_mod._current_user_lang.set("zh-Hans")
    ctx_mod._current_device_model.set("")
    ctx_mod._current_cancel_check.set(None)
    fake_ws = _FakeWSManager()
    ctx_mod._ws_manager = fake_ws

    # AI 写了一段无关的 text 但没改 main.gd
    async def _fake_agent(cwd, prompt, input_mode="touch", max_turns=20,
                         use_mcp=True, session_id=""):
        return ("here is some text but I forgot to write any files", "sid-x", 0)

    monkeypatch.setattr("robot.agent.tools.game_tools._run_claude_agent", _fake_agent)

    async def _fake_validate(*args, **kwargs):
        return ""
    monkeypatch.setattr("robot.agent.tools.game_tools._validate_and_fix", _fake_validate)

    upsert_called: list[bool] = []

    async def _fake_upsert(**kwargs):
        upsert_called.append(True)
    import robot.games.service as svc
    monkeypatch.setattr(svc, "upsert_game_row", _fake_upsert)

    from robot.agent.tools.game_tools import create_game
    result = await create_game.ainvoke({
        "title": "W", "description": "missing main.gd",
        "input_mode": "touch", "icon": "🎮",
        "template_game_id": "",
        "title_i18n": {"en": "W", "zh-Hans": "W"},
        "description_i18n": {"en": "w", "zh-Hans": "w"},
    })

    assert "fail" in result.lower() or "失败" in result, f"unexpected: {result!r}"
    game_dirs = [d for d in tmp_path.iterdir()
                 if d.is_dir() and d.name.startswith("ai_")]
    assert game_dirs == []
    assert list(tmp_path.glob("ai_*.zip")) == []
    assert upsert_called == []
    actions = [p["data"].get("action") for _, _, p in fake_ws.sent]
    assert "game_creation_failed" in actions
    assert "game_ready" not in actions
    failed_payload = next(p for _, _, p in fake_ws.sent
                          if p["data"].get("action") == "game_creation_failed")
    # 注意：result 非空但 main.gd 没改 → reason 应是 missing_main_files
    assert failed_payload["data"].get("reason") == "missing_main_files"

    ctx_mod._ws_manager = None

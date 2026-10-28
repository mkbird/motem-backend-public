"""streaming.py × FastPathOrchestrator 集成测试。

覆盖 4 条路径：
1. flag 关 → 完全不调 orchestrator
2. flag 开 + hit → orchestrator 命中 + slow path 不被调用
3. flag 开 + miss → 静默 fallthrough 到 slow path（用户无感）
4. template_game_id 给定 → 用户意图优先，跳过 fast path

create_game 是 langchain StructuredTool（pydantic 锁定 attr），不能直接
patch `.ainvoke`，要替换整个 symbol。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_create_tool(return_value: str = "slow path result") -> MagicMock:
    """构造一个能替换 `create_game` StructuredTool 的 MagicMock。"""
    t = MagicMock()
    t.ainvoke = AsyncMock(return_value=return_value)
    return t


def _make_extract_mock() -> AsyncMock:
    return AsyncMock(return_value=(
        {"en": "T", "zh-Hans": "测"},
        {"en": "D", "zh-Hans": "述"},
        "🎮",
        "",
    ))


@pytest.fixture
def fastpath_off():
    from robot.config import settings
    prev = settings.fastpath_enabled
    settings.fastpath_enabled = False
    yield
    settings.fastpath_enabled = prev


@pytest.fixture
def fastpath_on():
    from robot.config import settings
    prev = settings.fastpath_enabled
    settings.fastpath_enabled = True
    yield
    settings.fastpath_enabled = prev


@pytest.mark.asyncio
async def test_flag_off_skips_orchestrator(fastpath_off):
    """fastpath_enabled=False → 不调 _try_fastpath_create，直接 slow path。"""
    from robot.agent import streaming

    create_tool = _make_mock_create_tool("slow path result")
    extract_mock = _make_extract_mock()
    emit_mock = AsyncMock()
    fp_assert = AsyncMock(side_effect=AssertionError("must not be called when flag off"))

    with patch.object(streaming, "_emit", emit_mock), \
         patch("robot.agent.tools.game_tools.create_game", create_tool), \
         patch.object(streaming, "_extract_game_meta", extract_mock), \
         patch.object(streaming, "_try_fastpath_create", fp_assert):
        result = await streaming._handle_game_direct(
            "create", "", "做一个接苹果的游戏", emit_mock, input_mode="touch",
        )

    assert result == "slow path result"
    create_tool.ainvoke.assert_awaited_once()
    extract_mock.assert_awaited_once()
    fp_assert.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_hit_skips_slow_path(fastpath_on):
    """fastpath_enabled=True + hit → fast path 返回 + slow path 不调。"""
    from robot.agent import streaming

    create_tool = _make_mock_create_tool("SHOULD NOT BE CALLED")
    create_tool.ainvoke = AsyncMock(side_effect=AssertionError("slow path called on fast hit"))
    extract_mock = AsyncMock(side_effect=AssertionError("_extract_game_meta called on fast hit"))
    emit_mock = AsyncMock()
    fp_mock = AsyncMock(return_value="Created 测 (ai_1_abcdef)")

    with patch.object(streaming, "_emit", emit_mock), \
         patch("robot.agent.tools.game_tools.create_game", create_tool), \
         patch.object(streaming, "_extract_game_meta", extract_mock), \
         patch.object(streaming, "_try_fastpath_create", fp_mock):
        result = await streaming._handle_game_direct(
            "create", "", "做一个接苹果的游戏", emit_mock, input_mode="touch",
        )

    assert "ai_1_abcdef" in result
    fp_mock.assert_awaited_once()
    create_tool.ainvoke.assert_not_called()
    extract_mock.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_miss_falls_through_silently(fastpath_on):
    """fastpath_enabled=True + miss → 静默 fallthrough，用户感知不到 fast 失败。"""
    from robot.agent import streaming

    create_tool = _make_mock_create_tool("slow path result")
    extract_mock = _make_extract_mock()
    emit_mock = AsyncMock()
    fp_mock = AsyncMock(return_value=None)  # miss

    with patch.object(streaming, "_emit", emit_mock), \
         patch("robot.agent.tools.game_tools.create_game", create_tool), \
         patch.object(streaming, "_extract_game_meta", extract_mock), \
         patch.object(streaming, "_try_fastpath_create", fp_mock):
        result = await streaming._handle_game_direct(
            "create", "", "完全没有 archetype 匹配的描述", emit_mock, input_mode="touch",
        )

    assert result == "slow path result"
    fp_mock.assert_awaited_once()
    create_tool.ainvoke.assert_awaited_once()
    # 用户没收到任何 "fast path failed" 信号
    emit_args = [str(c.args) for c in emit_mock.await_args_list]
    assert all("fast" not in a.lower() for a in emit_args), \
        "user must not see fast-path failure"


@pytest.mark.asyncio
async def test_template_game_id_skips_fastpath(fastpath_on):
    """用户显式指定 template_game_id → 跳过 fast path 走 slow path（用户意图优先）。"""
    from robot.agent import streaming

    create_tool = _make_mock_create_tool("slow with template")
    extract_mock = _make_extract_mock()
    emit_mock = AsyncMock()
    fp_assert = AsyncMock(side_effect=AssertionError("fastpath must not run when template given"))

    with patch.object(streaming, "_emit", emit_mock), \
         patch("robot.agent.tools.game_tools.create_game", create_tool), \
         patch.object(streaming, "_extract_game_meta", extract_mock), \
         patch.object(streaming, "_try_fastpath_create", fp_assert):
        result = await streaming._handle_game_direct(
            "create", "", "anything", emit_mock,
            input_mode="touch",
            template_game_id="ai_1234567890_abcdef",
        )

    assert result == "slow with template"
    fp_assert.assert_not_called()
    create_tool.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_skip_fastpath_set_during_create_game(fastpath_on):
    """iOS 预调用块已试过 fast path → 调 create_game 时 _skip_fastpath=True，
    避免 create_game 内部重复尝试；调用后复位。"""
    from robot.agent import streaming
    from robot.agent.tools import game_tools

    seen = {}

    async def record(_args):
        seen["skip_during"] = game_tools._skip_fastpath.get()
        return "slow path result"

    create_tool = MagicMock()
    create_tool.ainvoke = AsyncMock(side_effect=record)
    extract_mock = _make_extract_mock()
    emit_mock = AsyncMock()
    fp_miss = AsyncMock(return_value=None)  # pre-block misses → slow path

    with patch.object(streaming, "_emit", emit_mock), \
         patch("robot.agent.tools.game_tools.create_game", create_tool), \
         patch.object(streaming, "_extract_game_meta", extract_mock), \
         patch.object(streaming, "_try_fastpath_create", fp_miss):
        await streaming._handle_game_direct(
            "create", "", "x", emit_mock, input_mode="touch")

    assert seen["skip_during"] is True
    assert game_tools._skip_fastpath.get() is False  # reset in finally

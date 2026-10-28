"""Tests for the per-message inactivity timeout in `_run_claude_agent`.

防 Sonnet extended thinking 死循环（21 min single API call, $1.32 wasted）—
通过 asyncio.wait_for 包每次 .__anext__()，超过 _INACTIVITY_TIMEOUT_SECONDS
就 abort 并优雅返回 ("", session_id)。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from robot.agent.tools import game_tools


# ─────────────────────────────────────────────────────────────────────
# Fakes for Claude Code SDK message types
# ─────────────────────────────────────────────────────────────────────


class _FakeAsyncGen:
    """Async iterator that yields one ResultMessage after `silence_seconds`
    of dead-air per __anext__(). Used to simulate Sonnet hanging on a single
    long API call."""

    def __init__(self, silence_seconds: float, then_yield):
        self._silence = silence_seconds
        self._delivered = False
        self._then_yield = then_yield

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._delivered:
            raise StopAsyncIteration
        await asyncio.sleep(self._silence)
        self._delivered = True
        return self._then_yield


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inactivity_timeout_aborts_silent_generator(monkeypatch):
    """模拟 Claude SDK 拉新消息的 anext 阻塞 > timeout → run_claude_agent 应优雅返回。"""
    # 把 timeout 调到很小以加速测试
    monkeypatch.setattr(
        game_tools, "_INACTIVITY_TIMEOUT_SECONDS", 0.3
    )

    # query() mock 出一个永远不发消息的 generator（10s sleep 远大于 0.3s timeout）
    def _mock_query(prompt, options):
        return _FakeAsyncGen(silence_seconds=10.0, then_yield=None)

    # `query` 在 _run_claude_agent 内部 from claude_code_sdk import → 必须 patch
    # 源模块的 query 属性。
    import claude_code_sdk
    monkeypatch.setattr(claude_code_sdk, "query", _mock_query)

    # 也 mock 掉 _send_game_progress_key 避免 ws_manager 依赖
    captured_progress: list[str] = []

    async def _fake_send(key, **kwargs):
        captured_progress.append(key)

    monkeypatch.setattr(game_tools, "_send_game_progress_key", _fake_send)

    t0 = asyncio.get_event_loop().time()
    result_text, session_id = await game_tools._run_claude_agent(
        cwd="/tmp", prompt="anything", input_mode="touch", max_turns=20,
        use_mcp=False, session_id="",
    )
    elapsed = asyncio.get_event_loop().time() - t0

    # 应在 ~ timeout 时间附近返回，远小于 mock 的 10s
    assert elapsed < 2.0, f"timeout 没生效，跑了 {elapsed}s"
    # 没有生成内容
    assert result_text == ""
    assert session_id == ""
    # 应推过 GAME_PROGRESS_TIMEOUT
    assert "GAME_PROGRESS_TIMEOUT" in captured_progress


@pytest.mark.asyncio
async def test_inactivity_timeout_passes_through_normal_messages(monkeypatch):
    """正常情况下，每条消息间隔很短，timeout 不应触发，run_claude_agent 应处理完所有消息。"""
    from claude_code_sdk import (
        AssistantMessage, ResultMessage, TextBlock,
    )

    monkeypatch.setattr(
        game_tools, "_INACTIVITY_TIMEOUT_SECONDS", 1.0
    )

    # 构造 1 条 AssistantMessage(TextBlock) + 1 条 ResultMessage 的迭代
    text_msg = AssistantMessage(content=[TextBlock(text="hello")], model="sonnet")
    result_msg = ResultMessage(
        subtype="success", duration_ms=100, duration_api_ms=80,
        is_error=False, num_turns=1, session_id="sess-fast",
        total_cost_usd=0.001, usage={}, result="hello",
    )

    async def _fast_gen(prompt, options):
        for m in (text_msg, result_msg):
            await asyncio.sleep(0.01)
            yield m

    import claude_code_sdk
    monkeypatch.setattr(claude_code_sdk, "query", _fast_gen)

    async def _fake_send(key, **kwargs):
        pass

    monkeypatch.setattr(game_tools, "_send_game_progress_key", _fake_send)

    result_text, session_id = await game_tools._run_claude_agent(
        cwd="/tmp", prompt="anything", input_mode="touch", max_turns=20,
        use_mcp=False, session_id="",
    )

    assert result_text == "hello"
    assert session_id == "sess-fast"


@pytest.mark.asyncio
async def test_inactivity_timeout_value_from_env(monkeypatch):
    """ROBOT_CODE_AGENT_INACTIVITY_TIMEOUT env 控制阈值。"""
    # reload 触发 env 读取
    monkeypatch.setenv("ROBOT_CODE_AGENT_INACTIVITY_TIMEOUT", "42.5")
    import importlib
    import robot.agent.tools.game_tools as gt
    importlib.reload(gt)
    try:
        assert gt._INACTIVITY_TIMEOUT_SECONDS == 42.5
    finally:
        # 还原
        monkeypatch.delenv("ROBOT_CODE_AGENT_INACTIVITY_TIMEOUT", raising=False)
        importlib.reload(gt)

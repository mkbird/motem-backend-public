"""Tests for the slim game agent (robot/bot/agent.py).

The LLM decision itself isn't unit-tested (it needs a model); instead we drive
``run_game_agent`` with a FAKE agent whose ``astream`` yields a scripted
sequence of messages, and assert the deterministic glue: game_id capture
(create via ToolMessage, modify via tool_calls, chat → None), active-game
update, memory save, and the pure prompt/history helpers.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from robot.bot import agent as bot_agent


# ── pure helpers ──

def test_history_to_messages():
    turns = [{"human": "hi", "ai": "hello"}, {"human": "make a game", "ai": ""}]
    msgs = bot_agent._history_to_messages(turns)
    assert [type(m).__name__ for m in msgs] == ["HumanMessage", "AIMessage", "HumanMessage"]
    assert msgs[0].content == "hi" and msgs[1].content == "hello"


def test_build_prompt_with_active_game():
    p = bot_agent._build_prompt({"user_lang": "zh-Hans", "game_id": "ai_1_abcdef"})
    assert "ai_1_abcdef" in p
    assert "用中文回复" in p


def test_build_prompt_without_active_game():
    p = bot_agent._build_prompt({"user_lang": "en", "game_id": ""})
    assert "no current game" in p
    assert "Reply in English." in p


# ── fake agent harness ──

class _FakeAgent:
    def __init__(self, chunks):
        self._chunks = chunks

    def astream(self, state, stream_mode=None):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()


@pytest.fixture
def patched(monkeypatch):
    """Stub active_game (redis) + memory; record their calls."""
    calls = {"set_active": [], "saved": []}

    async def _get_active(key):
        return calls.get("active")

    async def _set_active(key, gid):
        calls["set_active"].append((key, gid))

    monkeypatch.setattr(bot_agent.active_game, "get_active", _get_active)
    monkeypatch.setattr(bot_agent.active_game, "set_active", _set_active)
    monkeypatch.setattr(bot_agent, "set_request_context", lambda *a, **k: None)

    class _FakeMem:
        def __init__(self, key):
            self.key = key

        async def get_recent(self, limit=20):
            return []

        async def save_turn(self, human, ai):
            calls["saved"].append((human, ai))

    monkeypatch.setattr(bot_agent, "ChatMemoryClient", _FakeMem)
    return calls


def _run(agent, calls, text):
    return asyncio.run(bot_agent.run_game_agent(
        agent, requester_key="u1", owner_id="owner", text=text, user_lang="zh-Hans"))


def test_create_captures_game_id_from_tool_result(patched):
    agent = _FakeAgent([
        (AIMessage(content="", tool_calls=[
            {"name": "create_browser_game", "args": {"description": "catch"}, "id": "c1"}]), {}),
        (ToolMessage(content="游戏「接香蕉」创建完成！游戏ID: ai_1_abcdef", tool_call_id="c1"), {}),
        (AIMessage(content="做好啦！"), {}),
    ])
    reply, gid = _run(agent, patched, "做个接香蕉的游戏")
    assert reply == "做好啦！"
    assert gid == "ai_1_abcdef"
    assert patched["set_active"] == [("u1", "ai_1_abcdef")]
    assert patched["saved"] == [("做个接香蕉的游戏", "做好啦！")]


def test_modify_captures_game_id_from_tool_call_args(patched):
    agent = _FakeAgent([
        (AIMessage(content="", tool_calls=[
            {"name": "modify_game", "args": {"game_id": "ai_2_bbbbbb", "instruction": "faster"}, "id": "m1"}]), {}),
        (ToolMessage(content="Game updated. Modified 2 files: main.gd", tool_call_id="m1"), {}),
        (AIMessage(content="改好了！"), {}),
    ])
    reply, gid = _run(agent, patched, "敌人快点")
    assert reply == "改好了！"
    assert gid == "ai_2_bbbbbb"  # from tool-call args, since modify result has no id
    assert patched["set_active"] == [("u1", "ai_2_bbbbbb")]


def test_chat_returns_none_game_id(patched):
    agent = _FakeAgent([(AIMessage(content="你好呀～"), {})])
    reply, gid = _run(agent, patched, "你好")
    assert reply == "你好呀～"
    assert gid is None
    assert patched["set_active"] == []  # pure chat must not touch active game
    assert patched["saved"] == [("你好", "你好呀～")]


def test_create_browser_game_defaults_body_pose(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    captured = {}

    async def _fake_ainvoke(args):
        captured.update(args)
        return "游戏「X」创建完成！游戏ID: ai_9_aaaaaa"

    # create_game is a StructuredTool (pydantic-locked attrs) → replace the
    # whole symbol, not its .ainvoke.
    fake_create = MagicMock()
    fake_create.ainvoke = AsyncMock(side_effect=_fake_ainvoke)
    monkeypatch.setattr(bot_agent, "create_game", fake_create)

    out = asyncio.run(bot_agent.create_browser_game.ainvoke({"description": "a space shooter"}))
    assert "ai_9_aaaaaa" in out
    assert captured["input_mode"] == "body_pose"
    assert captured["description"] == "a space shooter"

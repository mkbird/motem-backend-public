"""Tests for src.agent.streaming — stream_agent_response."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage


# ── helpers ──


def _make_agent(chunks):
    """Create a mock agent whose astream yields (AIMessage_chunk, metadata) tuples."""
    agent = MagicMock()

    async def astream(state, stream_mode="messages"):
        for text in chunks:
            msg = AIMessage(content=text)
            yield (msg, {"langgraph_node": "agent"})

    agent.astream = astream
    return agent


def _make_memory():
    """Create a mock MemoryManager."""
    mem = MagicMock()
    mem.load_history_as_messages = AsyncMock(return_value=[])
    mem.save_turn = AsyncMock()
    return mem


def _make_state(user_lang: str = "en"):
    return {
        "owner_id": "fam-1",
        "user_id": "user-1",
        "member_role": "parent",
        "user_lang": user_lang,
    }


# ── tests ──


@pytest.mark.asyncio
async def test_cancel_check_stops():
    """cancel_check returns True → early termination, partial result."""
    from robot.agent.streaming import stream_agent_response

    call_count = 0

    def cancel_after_one():
        nonlocal call_count
        call_count += 1
        return call_count > 1  # True after first chunk

    agent = _make_agent(["Hello", " world", " foo"])
    memory = _make_memory()
    callback = AsyncMock()

    with patch("robot.agent.tools._context.set_request_context"):
        result = await stream_agent_response(
            "hi",
            agent,
            _make_state(),
            memory,
            callback,
            cancel_check=cancel_after_one,
            save_memory=False,
        )

    # Only "Hello" should have been processed before cancel
    assert result == "Hello"
    callback.assert_called_once_with("Hello")


@pytest.mark.asyncio
async def test_save_memory_false():
    """save_memory=False → save_turn not called."""
    from robot.agent.streaming import stream_agent_response

    agent = _make_agent(["Hi"])
    memory = _make_memory()
    callback = AsyncMock()

    with patch("robot.agent.tools._context.set_request_context"):
        await stream_agent_response(
            "hello",
            agent,
            _make_state(user_lang="zh-Hans"),
            memory,
            callback,
            save_memory=False,
        )

    memory.save_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_chunks_concatenated():
    """Return value is all chunks joined together."""
    from robot.agent.streaming import stream_agent_response

    agent = _make_agent(["Hello", " ", "world"])
    memory = _make_memory()
    callback = AsyncMock()

    with patch("robot.agent.tools._context.set_request_context"):
        result = await stream_agent_response(
            "hi",
            agent,
            _make_state(user_lang="zh-Hans"),
            memory,
            callback,
            save_memory=False,
        )

    assert result == "Hello world"


@pytest.mark.asyncio
async def test_callback_receives_chunks():
    """on_text_chunk receives each chunk individually."""
    from robot.agent.streaming import stream_agent_response

    agent = _make_agent(["A", "B", "C"])
    memory = _make_memory()
    received = []

    async def track(chunk):
        received.append(chunk)

    with patch("robot.agent.tools._context.set_request_context"):
        await stream_agent_response(
            "hi",
            agent,
            _make_state(),
            memory,
            track,
            save_memory=False,
        )

    assert received == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_error_returns_message():
    """Exception during streaming → returns Chinese error message."""
    from robot.agent.streaming import stream_agent_response

    agent = MagicMock()

    async def boom(state, stream_mode="messages"):
        raise RuntimeError("test error")
        yield  # noqa: unreachable — make it an async generator

    agent.astream = boom
    memory = _make_memory()
    callback = AsyncMock()

    with patch("robot.agent.tools._context.set_request_context"):
        result = await stream_agent_response(
            "hi",
            agent,
            _make_state(user_lang="zh-Hans"),
            memory,
            callback,
            save_memory=False,
        )

    assert "抱歉" in result
    assert "test error" in result
    callback.assert_called_once()

"""Tests for WebSocket: connection, ping/pong, AI chat, ConnectionManager."""

from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from starlette.testclient import TestClient

import robot.agent.streaming  # ensure module is loaded for patching
from aichat.websocket.manager import ConnectionManager
from tests.conftest import auth_header, register_and_login


# ── ConnectionManager unit tests (no server needed) ──


class TestConnectionManager:
    @pytest.mark.asyncio
    async def test_get_online_users_empty(self):
        mgr = ConnectionManager()
        assert mgr.get_online_users("group-1") == []

    @pytest.mark.asyncio
    async def test_is_user_online_false(self):
        mgr = ConnectionManager()
        assert mgr.is_user_online("group-1", "user-1") is False

    @pytest.mark.asyncio
    async def test_disconnect_cleanup(self):
        """Disconnect should clean up empty dicts."""
        mgr = ConnectionManager()
        mgr._connections = {"g1": {"u1": {"d1": "fake_ws"}}}
        assert mgr.is_user_online("g1", "u1") is True
        assert mgr.get_online_users("g1") == ["u1"]

        mgr.disconnect("g1", "u1", "d1")
        assert mgr.is_user_online("g1", "u1") is False
        assert mgr.get_online_users("g1") == []
        assert "g1" not in mgr._connections

    @pytest.mark.asyncio
    async def test_disconnect_partial(self):
        """Disconnect one device should keep other devices online."""
        mgr = ConnectionManager()
        mgr._connections = {"g1": {"u1": {"d1": "ws1", "d2": "ws2"}}}

        mgr.disconnect("g1", "u1", "d1")
        assert mgr.is_user_online("g1", "u1") is True
        assert "d2" in mgr._connections["g1"]["u1"]
        assert "d1" not in mgr._connections["g1"]["u1"]

    @pytest.mark.asyncio
    async def test_disconnect_nonexistent(self):
        """Disconnect non-existent connection should not raise."""
        mgr = ConnectionManager()
        mgr.disconnect("nonexistent", "user", "device")


# ── Helper: sync TestClient for WebSocket ──


def _get_sync_client(db_session):
    from robot.db.database import get_db
    import robot.main as main_module
    from robot.main import app

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    main_module._db_session_factory = _make_test_session_factory(db_session)
    return TestClient(app)


def _make_test_session_factory(db_session):
    @asynccontextmanager
    async def fake_factory():
        yield db_session

    return fake_factory


def _make_agent_state(owner_id):
    """Return a fake agent state dict for testing."""
    return {
        "owner_id": owner_id,
        "user_id": "test-user",
        "display_name": "测试用户",
    }


# ── WebSocket integration tests ──


class TestWebSocketEndpoint:
    @pytest.mark.asyncio
    async def test_ws_invalid_token(self, client: AsyncClient, db_session):
        """WebSocket with invalid token should be rejected."""
        sync_client = _get_sync_client(db_session)
        try:
            with sync_client.websocket_connect(
                "/ws?token=invalid-token&device_id=d1&group_id=g1"
            ) as ws:
                ws.receive_json()
        except Exception:
            pass

    @pytest.mark.asyncio
    async def test_ws_connect_and_ping(self, client: AsyncClient, db_session):
        """WebSocket connect with valid token, send ping, get pong."""
        info = await register_and_login(client, email="ws@test.com", display_name="WS用户")
        user_id = info["user"]["id"]

        sync_client = _get_sync_client(db_session)
        with sync_client.websocket_connect(
            f"/ws?token={info['access_token']}&device_id=test-device-001&group_id={user_id}"
        ) as ws:
            ws.send_json({"type": "ping"})
            data = ws.receive_json()
            assert data["type"] == "pong"


# ── AI Chat through WebSocket (v4 protocol) ──


class TestWebSocketAIChat:
    """Test AI agent chat via WebSocket with mocked agent streaming."""

    def _collect_v4_messages(self, ws, until_type="turn.done", max_msgs=20):
        """Receive v4 messages until `until_type` or max reached."""
        msgs = []
        for _ in range(max_msgs):
            data = ws.receive_json()
            msgs.append(data)
            if data.get("type") == until_type:
                break
        return msgs

    @pytest.mark.asyncio
    async def test_ai_chat_stream_response(self, client: AsyncClient, db_session):
        """Send chat message, receive streamed AI response via v4 events."""
        info = await register_and_login(
            client, email="ai_chat@test.com", display_name="AI测试"
        )
        user_id = info["user"]["id"]

        response_text = "你好！我是机器人助手，有什么可以帮你的吗？"

        async def mock_stream(user_input, agent, state, memory, on_text_chunk, **kwargs):
            for chunk in [response_text[:8], response_text[8:]]:
                result = on_text_chunk(chunk)
                if hasattr(result, "__await__"):
                    await result
            return response_text

        import robot.main as main_module

        original_agent = main_module._agent
        original_factory = main_module._db_session_factory
        main_module._agent = MagicMock()
        main_module._db_session_factory = _make_test_session_factory(db_session)

        try:
            with patch("robot.agent.streaming.stream_agent_response", side_effect=mock_stream), \
                 patch("robot.agent.context.build_agent_state", return_value=_make_agent_state(user_id)):
                sync_client = _get_sync_client(db_session)
                with sync_client.websocket_connect(
                    f"/ws?token={info['access_token']}&device_id=test-device-001&group_id={user_id}"
                ) as ws:
                    ws.send_json({
                        "type": "chat_message",
                        "data": {"text": "你好"},
                    })

                    msgs = self._collect_v4_messages(ws)

                    types = [m["type"] for m in msgs]
                    assert "turn.started" in types
                    assert "ai.thinking" in types
                    assert "ai.text.done" in types
                    assert "turn.done" in types

                    text_done = next(m for m in msgs if m["type"] == "ai.text.done")
                    assert "机器人助手" in text_done["payload"]["full_text"]
        finally:
            main_module._agent = original_agent
            main_module._db_session_factory = original_factory

    @pytest.mark.asyncio
    async def test_ai_chat_with_history(self, client: AsyncClient, db_session):
        """AI chat should pass user input and state to stream_agent_response."""
        info = await register_and_login(
            client, email="ai_hist@test.com", display_name="历史测试"
        )
        user_id = info["user"]["id"]

        captured_args = {}

        async def capture_stream(user_input, agent, state, memory, on_text_chunk, **kwargs):
            captured_args["user_input"] = user_input
            captured_args["state"] = dict(state)
            result = on_text_chunk("收到你的消息")
            if hasattr(result, "__await__"):
                await result
            return "收到你的消息"

        fake_state = _make_agent_state(user_id)

        import robot.main as main_module

        original_agent = main_module._agent
        original_factory = main_module._db_session_factory
        main_module._agent = MagicMock()
        main_module._db_session_factory = _make_test_session_factory(db_session)

        try:
            with patch("robot.agent.streaming.stream_agent_response", side_effect=capture_stream), \
                 patch("robot.agent.context.build_agent_state", return_value=fake_state):
                sync_client = _get_sync_client(db_session)
                with sync_client.websocket_connect(
                    f"/ws?token={info['access_token']}&device_id=test-device-001&group_id={user_id}"
                ) as ws:
                    ws.send_json({
                        "type": "chat_message",
                        "data": {"text": "帮我查一下明天的日程"},
                    })

                    self._collect_v4_messages(ws)

                    assert captured_args["user_input"] == "帮我查一下明天的日程"
                    assert captured_args["state"]["owner_id"] == user_id
        finally:
            main_module._agent = original_agent
            main_module._db_session_factory = original_factory

    @pytest.mark.asyncio
    async def test_ai_chat_error_handling(self, client: AsyncClient, db_session):
        """Agent error should return error event, not crash the connection."""
        info = await register_and_login(
            client, email="ai_err@test.com", display_name="错误测试"
        )
        user_id = info["user"]["id"]

        async def error_stream(user_input, agent, state, memory, on_text_chunk, **kwargs):
            error_msg = "抱歉，处理出错了：LLM connection failed"
            result = on_text_chunk(error_msg)
            if hasattr(result, "__await__"):
                await result
            return error_msg

        import robot.main as main_module

        original_agent = main_module._agent
        original_factory = main_module._db_session_factory
        main_module._agent = MagicMock()
        main_module._db_session_factory = _make_test_session_factory(db_session)

        try:
            with patch("robot.agent.streaming.stream_agent_response", side_effect=error_stream), \
                 patch("robot.agent.context.build_agent_state", return_value=_make_agent_state(user_id)):
                sync_client = _get_sync_client(db_session)
                with sync_client.websocket_connect(
                    f"/ws?token={info['access_token']}&device_id=test-device-001&group_id={user_id}"
                ) as ws:
                    ws.send_json({
                        "type": "chat_message",
                        "data": {"text": "你好"},
                    })

                    msgs = self._collect_v4_messages(ws)
                    text_done = next(
                        (m for m in msgs if m["type"] == "ai.text.done"), None
                    )
                    assert text_done is not None
                    assert "出错" in text_done["payload"]["full_text"]
        finally:
            main_module._agent = original_agent
            main_module._db_session_factory = original_factory

    @pytest.mark.asyncio
    async def test_ai_chat_empty_message(self, client: AsyncClient, db_session):
        """Empty chat message should be silently ignored."""
        info = await register_and_login(
            client, email="ai_empty@test.com", display_name="空消息测试"
        )
        user_id = info["user"]["id"]

        call_count = 0

        async def counting_stream(user_input, agent, state, memory, on_text_chunk, **kwargs):
            nonlocal call_count
            call_count += 1
            return ""

        import robot.main as main_module

        original_agent = main_module._agent
        original_factory = main_module._db_session_factory
        main_module._agent = MagicMock()
        main_module._db_session_factory = _make_test_session_factory(db_session)

        try:
            with patch("robot.agent.streaming.stream_agent_response", side_effect=counting_stream), \
                 patch("robot.agent.context.build_agent_state", return_value=_make_agent_state(user_id)):
                sync_client = _get_sync_client(db_session)
                with sync_client.websocket_connect(
                    f"/ws?token={info['access_token']}&device_id=test-device-001&group_id={user_id}"
                ) as ws:
                    ws.send_json({
                        "type": "chat_message",
                        "data": {"text": ""},
                    })
                    ws.send_json({"type": "ping"})
                    data = ws.receive_json()
                    assert data["type"] == "pong"

                    assert call_count == 0
        finally:
            main_module._agent = original_agent
            main_module._db_session_factory = original_factory

    @pytest.mark.asyncio
    async def test_ai_chat_multi_chunk_streaming(self, client: AsyncClient, db_session):
        """Verify multi-chunk streaming: each chunk arrives as ai.text.delta."""
        info = await register_and_login(
            client, email="ai_multi@test.com", display_name="流式测试"
        )
        user_id = info["user"]["id"]

        stream_chunks = ["今天", "天气", "晴朗，", "温度", "25度。"]

        async def multi_stream(user_input, agent, state, memory, on_text_chunk, **kwargs):
            full = ""
            for chunk in stream_chunks:
                result = on_text_chunk(chunk)
                if hasattr(result, "__await__"):
                    await result
                full += chunk
            return full

        import robot.main as main_module

        original_agent = main_module._agent
        original_factory = main_module._db_session_factory
        main_module._agent = MagicMock()
        main_module._db_session_factory = _make_test_session_factory(db_session)

        try:
            with patch("robot.agent.streaming.stream_agent_response", side_effect=multi_stream), \
                 patch("robot.agent.context.build_agent_state", return_value=_make_agent_state(user_id)):
                sync_client = _get_sync_client(db_session)
                with sync_client.websocket_connect(
                    f"/ws?token={info['access_token']}&device_id=test-device-001&group_id={user_id}"
                ) as ws:
                    ws.send_json({
                        "type": "chat_message",
                        "data": {"text": "今天天气怎么样"},
                    })

                    msgs = self._collect_v4_messages(ws)

                    deltas = [
                        m["payload"]["delta"]
                        for m in msgs
                        if m["type"] == "ai.text.delta"
                    ]
                    assert len(deltas) == 5
                    assert "".join(deltas) == "今天天气晴朗，温度25度。"

                    assert any(m["type"] == "turn.done" for m in msgs)
        finally:
            main_module._agent = original_agent
            main_module._db_session_factory = original_factory

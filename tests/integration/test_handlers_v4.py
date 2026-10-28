"""Tests for src.websocket.handlers — v4 event routing + legacy compatibility."""

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock proto/gRPC/opuslib only — keep real src.config to avoid redis URL issues.
_MOCKED_MODULES = [
    "src.proto", "src.proto.speech_services_pb2", "src.proto.speech_services_pb2_grpc",
    "src.proto.conversation_service_pb2", "src.proto.conversation_service_pb2_grpc",
    "aichat.proto", "aichat.proto.speech_pb2", "aichat.proto.speech_pb2_grpc",
    "opuslib",
]
for _mod in _MOCKED_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from aichat.config import AIChatConfig, configure
configure(AIChatConfig())

from aichat.websocket.protocol import ErrorCode
from aichat.websocket.session import SessionFSM, SessionState
from aichat.websocket.turn import Turn, TurnManager, TurnState


# ── helpers ──


def _make_session():
    """Build a mock Session with the attributes handlers.py touches."""
    session = MagicMock()
    session.fsm = SessionFSM()
    session.send_event = AsyncMock()
    session.send_error = AsyncMock()
    session.start_speech = AsyncMock()
    session.stop_speech = AsyncMock()
    session.close = AsyncMock()
    session._stop_speech_input = AsyncMock()
    session._cleanup_transport = MagicMock()
    session.track_task = MagicMock()
    session._loop = asyncio.get_event_loop()
    session._pending_speech_stop_id = None

    session.session_id = "sess-001"
    session.owner_id = "fam-001"
    session.user_id = "user-001"
    session.device_id = "dev-001"
    session.protocol_version = None
    session.codec_config = None
    session.dm_client = None
    session.business_handler = MagicMock()
    session.business_handler.handle_v4 = AsyncMock()
    session.business_handler.handle_legacy = AsyncMock()
    session.preempt_for_text = AsyncMock(side_effect=lambda t: t)

    # transport
    session.transport = MagicMock()
    session.transport.on_audio_input = MagicMock()

    # turn_manager — real object backed by the mock session
    tm = TurnManager(session)
    session.turn_manager = tm

    return session


@pytest.fixture(autouse=True)
def _noop():
    """Placeholder — no global state to inject after Protocol refactor."""
    yield


# ── binary frames ──


@pytest.mark.asyncio
async def test_binary_frame_to_transport():
    """Binary frame while SPEECH_ACTIVE → transport.on_audio_input called."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    session.fsm.state = SessionState.SPEECH_ACTIVE
    audio = b"\x00\x01\x02\x03"

    await handle_v4_message(session, audio)

    session.transport.on_audio_input.assert_called_once_with(audio)


@pytest.mark.asyncio
async def test_binary_ignored_non_speech():
    """Binary frame when not SPEECH_ACTIVE → ignored."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    session.fsm.state = SessionState.READY
    audio = b"\x00\x01"

    await handle_v4_message(session, audio)

    session.transport.on_audio_input.assert_not_called()


# ── session.init ──


@pytest.mark.asyncio
async def test_session_init():
    """session.init → FSM transitions to READY and session.ready reply sent."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    msg = json.dumps({
        "type": "session.init",
        "id": "req-1",
        "payload": {"protocol_version": 4},
    })

    await handle_v4_message(session, msg)

    assert session.fsm.state == SessionState.READY
    session.send_event.assert_called_once()
    call_args = session.send_event.call_args
    assert call_args[0][0] == "session.ready"
    assert call_args.kwargs.get("reply_to") == "req-1" or call_args[1].get("reply_to") == "req-1"


# ── speech.start ──


@pytest.mark.asyncio
async def test_speech_start():
    """speech.start → FSM → start_speech → speech.started."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    # Need READY state first
    session.fsm.state = SessionState.READY

    # After start_speech, codec_config should be set
    codec_mock = MagicMock()
    codec_mock.input_codec = "opus"
    codec_mock.output_codec = "opus"

    async def _set_codec(**kwargs):
        session.codec_config = codec_mock

    session.start_speech = AsyncMock(side_effect=_set_codec)

    msg = json.dumps({
        "type": "speech.start",
        "id": "req-2",
        "payload": {"mode": "full_duplex", "input_codec": "opus", "output_codec": "opus"},
    })

    await handle_v4_message(session, msg)

    assert session.fsm.state == SessionState.SPEECH_ACTIVE
    session.start_speech.assert_awaited_once()
    # speech.started should have been sent
    calls = [c[0][0] for c in session.send_event.call_args_list]
    assert "speech.started" in calls


# ── speech.stop cancel ──


@pytest.mark.asyncio
async def test_speech_stop_cancel():
    """speech.stop with action=cancel → stop_speech(cancel) + FSM + speech.stopped."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    session.fsm.state = SessionState.SPEECH_ACTIVE

    msg = json.dumps({
        "type": "speech.stop",
        "id": "req-3",
        "payload": {"action": "cancel"},
    })

    await handle_v4_message(session, msg)

    session.stop_speech.assert_awaited_once_with(action="cancel")
    assert session.fsm.state == SessionState.READY
    session.send_event.assert_called_once()
    assert session.send_event.call_args[0][0] == "speech.stopped"


# ── speech.stop graceful with active turn ──


@pytest.mark.asyncio
async def test_speech_stop_graceful_active_turn():
    """action=stop with active turn → sets _pending_speech_stop_id."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    session.fsm.state = SessionState.SPEECH_ACTIVE

    # Create an active turn in AI_PROCESSING state
    turn = Turn(turn_id="t-1", trigger="voice", session=session)
    turn.state = TurnState.AI_PROCESSING
    session.turn_manager.active_turn = turn

    msg = json.dumps({
        "type": "speech.stop",
        "id": "req-4",
        "payload": {"action": "stop"},
    })

    await handle_v4_message(session, msg)

    session._stop_speech_input.assert_awaited_once()
    assert session._pending_speech_stop_id == "req-4"
    # FSM should NOT have transitioned yet
    assert session.fsm.state == SessionState.SPEECH_ACTIVE


# ── speech.stop graceful with no active turn ──


@pytest.mark.asyncio
async def test_speech_stop_graceful_no_turn():
    """action=stop without active turn → immediate cleanup + speech.stopped."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    session.fsm.state = SessionState.SPEECH_ACTIVE

    msg = json.dumps({
        "type": "speech.stop",
        "id": "req-5",
        "payload": {"action": "stop"},
    })

    await handle_v4_message(session, msg)

    session._stop_speech_input.assert_awaited_once()
    session._cleanup_transport.assert_called_once()
    assert session.fsm.state == SessionState.READY
    session.send_event.assert_called_once()
    assert session.send_event.call_args[0][0] == "speech.stopped"


# ── text.send ──


@pytest.mark.asyncio
async def test_text_send():
    """text.send → creates Turn + turn.started + starts process_turn_response."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    session.fsm.state = SessionState.READY

    msg = json.dumps({
        "type": "text.send",
        "payload": {"text": "你好"},
    })

    with patch("aichat.speech.tts_pipeline.process_turn_response", new_callable=AsyncMock) as mock_ptr:
        await handle_v4_message(session, msg)

    # turn.started should have been sent
    assert session.send_event.call_args[0][0] == "turn.started"
    # track_task should have been called
    session.track_task.assert_called_once()


@pytest.mark.asyncio
async def test_text_send_tts_flag():
    """payload.tts passed through to process_turn_response."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    session.fsm.state = SessionState.READY

    msg = json.dumps({
        "type": "text.send",
        "payload": {"text": "你好", "tts": False},
    })

    with patch("aichat.speech.tts_pipeline.process_turn_response", new_callable=AsyncMock) as mock_ptr:
        await handle_v4_message(session, msg)
        # Let the task run
        await asyncio.sleep(0.05)

        # The task was created with tts_enabled=False
        # Check the process_turn_response call args through the task
        mock_ptr.assert_called_once()
        call_args = mock_ptr.call_args
        assert call_args[0][3] is False  # tts_enabled


# ── session.close ──


@pytest.mark.asyncio
async def test_session_close():
    """session.close → calls session.close()."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    session.fsm.state = SessionState.READY

    msg = json.dumps({"type": "session.close"})

    await handle_v4_message(session, msg)

    session.close.assert_awaited_once()


# ── ping / pong ──


@pytest.mark.asyncio
async def test_ping_pong():
    """ping → pong response."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    session.fsm.state = SessionState.READY

    msg = json.dumps({"type": "ping"})

    await handle_v4_message(session, msg)

    session.send_event.assert_called_once_with("pong", {})


# ── unknown event ──


@pytest.mark.asyncio
async def test_unknown_event_error():
    """Unknown event type → INVALID_MESSAGE error."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    session.fsm.state = SessionState.READY

    msg = json.dumps({"type": "totally_unknown"})

    await handle_v4_message(session, msg)

    session.send_error.assert_awaited_once()
    assert session.send_error.call_args[0][0] == ErrorCode.INVALID_MESSAGE


# ── Robot events ──


@pytest.mark.asyncio
async def test_robot_shopping_delegates():
    """robot.shopping.update → business_handler.handle_v4 called."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    session.fsm.state = SessionState.READY

    msg = json.dumps({
        "type": "robot.shopping.update",
        "payload": {"items": ["eggs"]},
    })

    await handle_v4_message(session, msg)
    session.business_handler.handle_v4.assert_awaited_once()
    call_args = session.business_handler.handle_v4.call_args
    assert call_args[0][1] == "robot.shopping.update"


@pytest.mark.asyncio
async def test_robot_device_delegates():
    """robot.device.command → business_handler.handle_v4 called."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    session.fsm.state = SessionState.READY

    msg = json.dumps({
        "type": "robot.device.command",
        "payload": {"cmd": "turn_on"},
    })

    await handle_v4_message(session, msg)
    session.business_handler.handle_v4.assert_awaited_once()
    call_args = session.business_handler.handle_v4.call_args
    assert call_args[0][1] == "robot.device.command"


@pytest.mark.asyncio
async def test_robot_no_handler_error():
    """robot.* event without business_handler → INVALID_MESSAGE error."""
    from aichat.websocket.handlers import handle_v4_message

    session = _make_session()
    session.fsm.state = SessionState.READY
    session.business_handler = None

    msg = json.dumps({"type": "robot.foo.bar", "payload": {}})

    await handle_v4_message(session, msg)

    session.send_error.assert_awaited_once()
    assert session.send_error.call_args[0][0] == ErrorCode.INVALID_MESSAGE


# ── Legacy handler ──


@pytest.mark.asyncio
async def test_legacy_chat_routes_v4():
    """Legacy chat_message → creates Turn pipeline."""
    from aichat.websocket.handlers import handle_legacy_message

    session = _make_session()
    session.fsm.state = SessionState.READY

    data = {"type": "chat_message", "data": {"text": "你好世界"}}

    with patch("aichat.speech.tts_pipeline.process_turn_response", new_callable=AsyncMock):
        await handle_legacy_message(session, data)

    # turn.started event should have been sent
    assert session.send_event.call_args[0][0] == "turn.started"
    session.track_task.assert_called_once()


@pytest.mark.asyncio
async def test_legacy_empty_ignored():
    """Empty text in legacy chat_message → ignored (no turn created)."""
    from aichat.websocket.handlers import handle_legacy_message

    session = _make_session()
    data = {"type": "chat_message", "data": {"text": ""}}

    await handle_legacy_message(session, data)

    session.send_event.assert_not_called()
    session.track_task.assert_not_called()

"""Integration tests for Session — real Session + mock WebSocket + patched gRPC."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Mock proto stubs + redis chain to avoid requiring compiled protobuf or Redis at test time.
_MOCKED_MODULES = [
    "speech_pb2", "speech_pb2_grpc",
    "src.proto", "src.proto.speech_pb2", "src.proto.speech_pb2_grpc",
    "aichat.proto", "aichat.proto.speech_pb2", "aichat.proto.speech_pb2_grpc",
    "opuslib",
]
for _mod in _MOCKED_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Mock redis modules to prevent robot.db.redis from connecting at import time.
_REDIS_MOCKS = [
    "robot.db.redis", "robot.memory.redis_client",
    "src.db.redis", "src.memory.redis_client",
]
for _mod in _REDIS_MOCKS:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from aichat.config import AIChatConfig, configure
configure(AIChatConfig())

import pytest

from aichat.websocket.session import Session, SessionState


def _make_mock_websocket():
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    ws.send_bytes = AsyncMock()
    return ws


def _make_mock_asr():
    """Create a mock RecognitionStream."""
    asr = AsyncMock()
    asr.begin = AsyncMock()
    asr.write_audio = AsyncMock()
    asr.end = AsyncMock(return_value="")
    asr._active = False
    return asr


def _make_mock_dm():
    """Create a mock DialogueManager."""
    dm = MagicMock()
    dm.activate = MagicMock()
    dm.deactivate = MagicMock()
    dm.feed_audio = MagicMock()
    dm.feed_transcript = MagicMock()
    dm.notify_playback = MagicMock()
    dm.clear_state = MagicMock()
    return dm


def _make_mock_transport():
    transport = MagicMock()
    transport.start = MagicMock()
    transport.stop = MagicMock()
    transport.read_audio = AsyncMock(side_effect=asyncio.CancelledError)
    transport.write_audio = MagicMock()
    transport.clear_audio_queue = MagicMock()
    return transport


@pytest.fixture
def mock_ws():
    return _make_mock_websocket()


@pytest.fixture
def patches():
    """Patch all external dependencies of Session."""
    mock_asr = _make_mock_asr()
    mock_dm = _make_mock_dm()
    mock_transport = _make_mock_transport()

    with patch("aichat.websocket.session.RecognitionStream", return_value=mock_asr) as asr_cls, \
         patch("aichat.websocket.session.DialogueManager", return_value=mock_dm) as dm_cls, \
         patch("aichat.websocket.session.AudioTransport", return_value=mock_transport) as transport_cls, \
         patch("aichat.websocket.session.CodecConfig") as codec_cls, \
         patch("aichat.speech.tts_pipeline.process_turn_response", new_callable=AsyncMock) as mock_ptr:

        codec_instance = MagicMock()
        codec_instance.decode_input = MagicMock(side_effect=lambda d: d)
        codec_instance.encode_output = MagicMock(side_effect=lambda d: d)
        codec_cls.return_value = codec_instance

        yield {
            "asr": mock_asr,
            "dm": mock_dm,
            "transport": mock_transport,
            "codec": codec_instance,
            "process_turn_response": mock_ptr,
            "asr_cls": asr_cls,
            "dm_cls": dm_cls,
        }


async def _make_session(mock_ws, patches) -> Session:
    session = Session(
        websocket=mock_ws,
        group_id="test-group",
        user_id="test-user",
        device_id="test-device",
        llm_streamer=MagicMock(stream=AsyncMock(return_value="test response")),
        agent_state_provider=MagicMock(build_state=AsyncMock(return_value={})),
        memory_factory=MagicMock(return_value=MagicMock(save_turn=AsyncMock())),
        business_handler=None,
    )
    session.fsm.transition("session.init")
    return session


@pytest.mark.asyncio
async def test_full_duplex_lifecycle(mock_ws, patches):
    """start_speech -> verify resources -> stop_speech -> verify cleanup."""
    session = await _make_session(mock_ws, patches)

    await session.start_speech(mode="full_duplex", input_codec="pcm", output_codec="pcm")

    assert session.asr_client is not None
    assert session.dm is not None
    assert session.transport is not None
    assert session.speech_mode == "full_duplex"
    patches["asr"].begin.assert_called_once()
    patches["dm"].activate.assert_called_once()

    await session.stop_speech(action="cancel")

    assert session.asr_client is None
    assert session.transport is None
    assert session.speech_mode is None


@pytest.mark.asyncio
async def test_asr_only_lifecycle(mock_ws, patches):
    """start_speech(asr_only) -> no dm."""
    session = await _make_session(mock_ws, patches)

    await session.start_speech(mode="asr_only", input_codec="pcm", output_codec="pcm")

    assert session.asr_client is not None
    assert session.dm is None
    assert session.speech_mode == "asr_only"

    await session.stop_speech(action="cancel")


@pytest.mark.asyncio
async def test_asr_partial_creates_turn(mock_ws, patches):
    """full_duplex partial -> creates Turn + asr.partial."""
    session = await _make_session(mock_ws, patches)
    await session.start_speech(mode="full_duplex", input_codec="pcm", output_codec="pcm")

    await session._handle_asr_sentence("hello", is_sentence_end=False)

    assert session.turn_manager.active_turn is not None
    turn = session.turn_manager.active_turn
    assert turn.trigger == "voice"

    calls = mock_ws.send_json.call_args_list
    assert any("turn.started" in str(c) for c in calls)
    assert any("asr.partial" in str(c) for c in calls)

    await session.stop_speech(action="cancel")


@pytest.mark.asyncio
async def test_asr_feeds_dm_transcript(mock_ws, patches):
    """full_duplex ASR result → dm.feed_transcript called."""
    session = await _make_session(mock_ws, patches)
    await session.start_speech(mode="full_duplex", input_codec="pcm", output_codec="pcm")

    await session._handle_asr_sentence("hello", is_sentence_end=True, begin_time=100, end_time=500)

    patches["dm"].feed_transcript.assert_called_once_with(
        "hello", is_final=True, start_ms=100, end_ms=500,
    )

    await session.stop_speech(action="cancel")


@pytest.mark.asyncio
async def test_dm_intent_triggers_llm(mock_ws, patches):
    """DM intent -> asr.final + process_turn_response."""
    session = await _make_session(mock_ws, patches)
    await session.start_speech(mode="full_duplex", input_codec="pcm", output_codec="pcm")

    await session._handle_asr_sentence("turn on the lights", is_sentence_end=False)
    assert session.turn_manager.active_turn is not None

    await session._handle_dm_intent(
        text="turn on the lights",
        is_complete=True,
        confidence=0.95,
        reasoning="user_intent",
    )

    calls = mock_ws.send_json.call_args_list
    assert any("asr.final" in str(c) for c in calls)

    await session.stop_speech(action="cancel")


@pytest.mark.asyncio
async def test_dm_intent_empty_text_ignored(mock_ws, patches):
    """empty text intent -> no LLM."""
    session = await _make_session(mock_ws, patches)
    await session.start_speech(mode="full_duplex", input_codec="pcm", output_codec="pcm")

    turn = await session.turn_manager.create_turn(trigger="voice")
    session.accumulator.reset() if hasattr(session.accumulator, "reset") else None

    initial_call_count = mock_ws.send_json.call_count

    await session._handle_dm_intent(
        text="", is_complete=True, confidence=0.9, reasoning="timeout",
    )

    new_calls = mock_ws.send_json.call_args_list[initial_call_count:]
    asr_final_calls = [c for c in new_calls if "asr.final" in str(c)]
    assert len(asr_final_calls) == 0

    await session.stop_speech(action="cancel")


@pytest.mark.asyncio
async def test_dm_interrupt_cancels_restarts(mock_ws, patches):
    """interrupt -> cancel + new Turn."""
    session = await _make_session(mock_ws, patches)
    await session.start_speech(mode="full_duplex", input_codec="pcm", output_codec="pcm")

    await session._handle_asr_sentence("hello", is_sentence_end=False)
    original_turn = session.turn_manager.active_turn
    assert original_turn is not None

    await session._handle_dm_interrupt()

    assert original_turn.cancelled is True
    calls = mock_ws.send_json.call_args_list
    assert any("turn.cancelled" in str(c) for c in calls)

    await session.stop_speech(action="cancel")


@pytest.mark.asyncio
async def test_stop_cancel_cancels_turn(mock_ws, patches):
    """stop_speech(cancel) -> cancels active Turn."""
    session = await _make_session(mock_ws, patches)
    await session.start_speech(mode="full_duplex", input_codec="pcm", output_codec="pcm")

    await session._handle_asr_sentence("test", is_sentence_end=False)
    turn = session.turn_manager.active_turn
    assert turn is not None

    await session.stop_speech(action="cancel")

    assert turn.cancelled is True
    assert session.asr_client is None
    assert session.transport is None


@pytest.mark.asyncio
async def test_stop_graceful_waits(mock_ws, patches):
    """stop_speech(stop) + active turn -> pending."""
    session = await _make_session(mock_ws, patches)
    await session.start_speech(mode="full_duplex", input_codec="pcm", output_codec="pcm")

    await session._handle_asr_sentence("test", is_sentence_end=False)
    turn = session.turn_manager.active_turn
    assert turn is not None

    await session.stop_speech(action="stop")

    assert turn.cancelled is False
    assert session.asr_client is None


@pytest.mark.asyncio
async def test_close_releases_all(mock_ws, patches):
    """close() -> all resources released."""
    session = await _make_session(mock_ws, patches)
    await session.start_speech(mode="full_duplex", input_codec="pcm", output_codec="pcm")

    await session._handle_asr_sentence("test", is_sentence_end=False)

    await session.close()

    assert session.fsm.state == SessionState.CLOSED
    assert session.speech_mode is None

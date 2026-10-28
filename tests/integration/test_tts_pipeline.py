"""Tests for aichat.speech.tts_pipeline — process_turn_response and TTS callbacks."""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Mock proto/gRPC/opuslib
_MOCKED_MODULES = [
    "aichat.proto", "aichat.proto.speech_pb2", "aichat.proto.speech_pb2_grpc",
    "src.proto", "src.proto.speech_pb2", "src.proto.speech_pb2_grpc",
    "opuslib",
]
for _mod in _MOCKED_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from aichat.config import AIChatConfig, configure
from aichat.websocket.turn import Turn, TurnManager, TurnState

# Configure aichat SDK for tests
configure(AIChatConfig())


# ── helpers ──


def _make_session():
    """Build a mock Session suitable for tts_pipeline tests."""
    session = MagicMock()
    session.send_event = AsyncMock()
    session.send_error = AsyncMock()
    session.get_agent_state = AsyncMock(return_value={
        "owner_id": "fam-1",
        "user_id": "user-1",
        "member_role": "parent",
    })
    session.session_id = "sess-1"
    session.user_id = "user-1"
    session.device_id = "dev-1"
    session.dm = None
    session.transport = MagicMock()
    session._loop = asyncio.get_event_loop()
    session._pending_speech_stop_id = None
    session._finalize_speech_stop = AsyncMock()

    # Protocol injections
    mock_memory = MagicMock(save_turn=AsyncMock())
    session.memory_factory = MagicMock(return_value=mock_memory)
    session.llm_streamer = MagicMock()
    session.llm_streamer.stream = AsyncMock(return_value="hello")

    tm = TurnManager(session)
    session.turn_manager = tm
    return session


def _make_turn(session, turn_id="turn-1"):
    turn = Turn(turn_id=turn_id, trigger="text", session=session)
    session.turn_manager.turns[turn_id] = turn
    session.turn_manager.active_turn = turn
    return turn


# ── process_turn_response tests ──


@pytest.mark.asyncio
async def test_sends_ai_thinking():
    """First event emitted is ai.thinking."""
    from aichat.speech.tts_pipeline import process_turn_response

    session = _make_session()
    turn = _make_turn(session)

    await process_turn_response(session, turn, "hi", tts_enabled=False)

    first_call = session.send_event.call_args_list[0]
    assert first_call[0][0] == "ai.thinking"


@pytest.mark.asyncio
async def test_creates_tts_client():
    """tts_enabled=True → SpeechStream created and started."""
    from aichat.speech.tts_pipeline import process_turn_response

    session = _make_session()
    turn = _make_turn(session)

    with patch("aichat.speech.tts_pipeline.SpeechStream") as MockTTS:
        tts_instance = MagicMock()
        MockTTS.return_value = tts_instance
        await process_turn_response(session, turn, "hi", tts_enabled=True)

    MockTTS.assert_called_once()
    tts_instance.begin.assert_called_once()


@pytest.mark.asyncio
async def test_no_tts_when_disabled():
    """tts_enabled=False → no TTS client created."""
    from aichat.speech.tts_pipeline import process_turn_response

    session = _make_session()
    turn = _make_turn(session)

    with patch("aichat.speech.tts_pipeline.SpeechStream") as MockTTS:
        await process_turn_response(session, turn, "hi", tts_enabled=False)

    MockTTS.assert_not_called()
    assert turn.tts_client is None


@pytest.mark.asyncio
async def test_streams_chunks():
    """Each LLM chunk → ai.text.delta + tts.write_text."""
    from aichat.speech.tts_pipeline import process_turn_response

    session = _make_session()
    turn = _make_turn(session)

    async def fake_stream(text, state, on_chunk, cancel_check=None, attachments=None):
        for c in ["Hello", " world"]:
            await on_chunk(c)
        return "Hello world"

    session.llm_streamer.stream = AsyncMock(side_effect=fake_stream)

    with patch("aichat.speech.tts_pipeline.SpeechStream") as MockTTS:
        tts_instance = MagicMock()
        MockTTS.return_value = tts_instance
        await process_turn_response(session, turn, "hi", tts_enabled=True)

    delta_calls = [c for c in session.send_event.call_args_list if c[0][0] == "ai.text.delta"]
    assert len(delta_calls) == 2
    assert delta_calls[0][0][1]["delta"] == "Hello"
    assert delta_calls[1][0][1]["delta"] == " world"
    assert tts_instance.write_text.call_count == 2


@pytest.mark.asyncio
async def test_cancel_skips_chunks():
    """turn.cancelled → on_ai_chunk does nothing."""
    from aichat.speech.tts_pipeline import process_turn_response

    session = _make_session()
    turn = _make_turn(session)
    turn.cancelled = True

    async def fake_stream(text, state, on_chunk, cancel_check=None, attachments=None):
        await on_chunk("should be skipped")
        return "should be skipped"

    session.llm_streamer.stream = AsyncMock(side_effect=fake_stream)

    await process_turn_response(session, turn, "hi", tts_enabled=False)

    delta_calls = [c for c in session.send_event.call_args_list if c[0][0] == "ai.text.delta"]
    assert len(delta_calls) == 0


@pytest.mark.asyncio
async def test_sends_ai_text_done():
    """After streaming, ai.text.done event emitted with full_text."""
    from aichat.speech.tts_pipeline import process_turn_response

    session = _make_session()
    turn = _make_turn(session)
    session.llm_streamer.stream = AsyncMock(return_value="final text")

    await process_turn_response(session, turn, "hi", tts_enabled=False)

    done_calls = [c for c in session.send_event.call_args_list if c[0][0] == "ai.text.done"]
    assert len(done_calls) == 1
    assert done_calls[0][0][1]["full_text"] == "final text"


@pytest.mark.asyncio
async def test_drains_tts():
    """After stream completes, tts_client.seal() + drain() are called."""
    from aichat.speech.tts_pipeline import process_turn_response

    session = _make_session()
    turn = _make_turn(session)

    with patch("aichat.speech.tts_pipeline.SpeechStream") as MockTTS:
        tts_instance = MagicMock()
        MockTTS.return_value = tts_instance
        await process_turn_response(session, turn, "hi", tts_enabled=True)

    tts_instance.seal.assert_called_once()
    tts_instance.drain.assert_called_once()


@pytest.mark.asyncio
async def test_completes_turn_no_tts():
    """No TTS → directly completes turn (turn.done event)."""
    from aichat.speech.tts_pipeline import process_turn_response

    session = _make_session()
    turn = _make_turn(session)

    await process_turn_response(session, turn, "hi", tts_enabled=False)

    done_calls = [c for c in session.send_event.call_args_list if c[0][0] == "turn.done"]
    assert len(done_calls) == 1


@pytest.mark.asyncio
async def test_saves_memory():
    """On success, save_turn(input, response) is called."""
    from aichat.speech.tts_pipeline import process_turn_response

    session = _make_session()
    turn = _make_turn(session)
    session.llm_streamer.stream = AsyncMock(return_value="answer")
    mem_instance = MagicMock(save_turn=AsyncMock())
    session.memory_factory = MagicMock(return_value=mem_instance)

    await process_turn_response(session, turn, "question", tts_enabled=False)

    mem_instance.save_turn.assert_awaited_once_with("question", "answer")


@pytest.mark.asyncio
async def test_no_memory_on_cancel():
    """Cancelled turn → save_turn not called."""
    from aichat.speech.tts_pipeline import process_turn_response

    session = _make_session()
    turn = _make_turn(session)
    turn.cancelled = True
    mem_instance = MagicMock(save_turn=AsyncMock())
    session.memory_factory = MagicMock(return_value=mem_instance)

    await process_turn_response(session, turn, "hi", tts_enabled=False)

    mem_instance.save_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_handling():
    """Exception → send_error + complete_turn."""
    from aichat.speech.tts_pipeline import process_turn_response

    session = _make_session()
    turn = _make_turn(session)
    session.llm_streamer.stream = AsyncMock(side_effect=RuntimeError("boom"))

    await process_turn_response(session, turn, "hi", tts_enabled=False)

    session.send_error.assert_awaited_once()
    assert turn.state == TurnState.COMPLETED


@pytest.mark.asyncio
async def test_dm_notify_playback():
    """TTS start → dm.notify_playback(True) called."""
    from aichat.speech.tts_pipeline import process_turn_response

    session = _make_session()
    turn = _make_turn(session)
    session.dm = MagicMock()
    session.dm.notify_playback = MagicMock()

    with patch("aichat.speech.tts_pipeline.SpeechStream") as MockTTS:
        tts_instance = MagicMock()
        MockTTS.return_value = tts_instance
        await process_turn_response(session, turn, "hi", tts_enabled=True)

    session.dm.notify_playback.assert_called_with(True)


# ── _on_tts_audio tests ──


def test_first_chunk_tts_start():
    """First audio chunk → tts.start event + tts_started flag."""
    from aichat.speech.tts_pipeline import _on_tts_audio

    session = _make_session()
    turn = _make_turn(session)
    turn.tts_started = False
    session._loop = MagicMock()

    with patch("asyncio.run_coroutine_threadsafe") as mock_rcs:
        mock_rcs.return_value = MagicMock()
        mock_rcs.return_value.result = MagicMock()
        _on_tts_audio(session, turn, b"\x00" * 100)

    assert turn.tts_started is True
    assert turn.state == TurnState.TTS_STREAMING
    assert turn.tts_bytes_sent == 100


def test_cancelled_ignores_audio():
    """Cancelled turn → _on_tts_audio does nothing."""
    from aichat.speech.tts_pipeline import _on_tts_audio

    session = _make_session()
    turn = _make_turn(session)
    turn.cancelled = True
    turn.tts_bytes_sent = 0

    _on_tts_audio(session, turn, b"\x00" * 50)

    assert turn.tts_bytes_sent == 0
    assert not turn.tts_started


# ── on_playback_end tests ──


@pytest.mark.asyncio
async def test_playback_end_tts_done():
    """on_playback_end → sends tts.done + complete_turn."""
    from aichat.speech.tts_pipeline import on_playback_end

    session = _make_session()
    turn = _make_turn(session)
    turn.tts_bytes_sent = 4800
    session.turn_manager.set_tts_turn(turn)

    await on_playback_end(session)

    tts_done_calls = [c for c in session.send_event.call_args_list if c[0][0] == "tts.done"]
    assert len(tts_done_calls) == 1
    assert tts_done_calls[0][0][1]["audio_bytes_total"] == 4800

    turn_done_calls = [c for c in session.send_event.call_args_list if c[0][0] == "turn.done"]
    assert len(turn_done_calls) == 1


@pytest.mark.asyncio
async def test_playback_end_no_turn_noop():
    """on_playback_end with no tts_turn → no-op."""
    from aichat.speech.tts_pipeline import on_playback_end

    session = _make_session()
    await on_playback_end(session)
    session.send_event.assert_not_called()

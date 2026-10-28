"""REST chat endpoint — used by Share Extension and other non-WebSocket clients."""

import logging

from fastapi import APIRouter, Depends

from robot.auth.dependencies import get_current_user
from robot.chat.models import ChatRequest, ChatResponse
from robot.db.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Set by main.py on startup
_agent = None
_db_session_factory = None


def set_dependencies(agent, db_session_factory):
    global _agent, _db_session_factory
    _agent = agent
    _db_session_factory = db_session_factory


@router.post("/message", response_model=ChatResponse)
async def send_message(
    req: ChatRequest,
    user: User = Depends(get_current_user),
):
    """Send a message to the AI agent and get a complete response."""
    if not _agent or not _db_session_factory:
        return ChatResponse(text="AI Agent 未初始化")

    from robot.agent.context import build_agent_state
    from robot.agent.streaming import stream_agent_response
    from robot.memory.memory_manager import MemoryManager

    state = await build_agent_state(
        _db_session_factory, req.owner_id, str(user.id), req.device_id
    )
    memory = MemoryManager(str(user.id))

    # Collect full response (no streaming for REST)
    response = await stream_agent_response(
        req.text, _agent, state, memory, on_text_chunk=lambda _: None
    )

    return ChatResponse(text=response)

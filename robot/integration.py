"""胶水层 — 实现 aichat Protocol，将 Robot 业务能力注入 aichat SDK。"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import WebSocket

from aichat.protocols import (
    AgentStateProvider,
    BusinessEventHandler,
    LLMStreamer,
    MemoryFactory,
    MemoryStore,
)
from robot.websocket import ws_manager
from aichat.websocket.session import Session

logger = logging.getLogger(__name__)


class RobotLLMStreamer:
    """实现 LLMStreamer — 委托给 agent.streaming。"""

    def __init__(self, agent):
        self._agent = agent

    async def stream(
        self,
        user_input: str,
        agent_state: dict,
        on_chunk: Callable[[str], Any],
        cancel_check: Callable[[], bool] | None = None,
        attachments: list | None = None,
    ) -> str:
        from robot.agent.streaming import stream_agent_response
        from robot.memory.memory_manager import MemoryManager

        memory = MemoryManager(agent_state.get("user_id", ""))
        return await stream_agent_response(
            user_input,
            self._agent,
            agent_state,
            memory,
            on_chunk,
            cancel_check=cancel_check,
            save_memory=False,
            attachments=attachments,
        )


class RobotAgentStateProvider:
    """实现 AgentStateProvider。"""

    def __init__(self, db_factory, user_lang: str = "en", device_model: str = ""):
        self._db = db_factory
        self._user_lang = user_lang
        self._device_model = device_model

    async def build_state(
        self, group_id: str, user_id: str, device_id: str
    ) -> dict:
        from robot.agent.context import build_agent_state

        return await build_agent_state(
            self._db, group_id, user_id, device_id,
            user_lang=self._user_lang,
            device_model=self._device_model,
        )


class RobotMemoryStore:
    def __init__(self, user_id: str):
        from robot.memory.memory_manager import MemoryManager

        self._mm = MemoryManager(user_id)

    async def save_turn(self, human_msg: str, ai_msg: str) -> None:
        await self._mm.save_turn(human_msg, ai_msg)


class RobotMemoryFactory:
    def __call__(self, user_id: str) -> MemoryStore:
        return RobotMemoryStore(user_id)


class RobotBusinessHandler:
    """处理 robot.* 业务事件。"""

    async def handle_v4(
        self, session: Any, event_type: str, msg: dict
    ) -> None:
        from aichat.websocket.protocol import ErrorCode

        payload = msg.get("payload", {})

        match event_type:
            case "robot.device.command":
                await ws_manager.send_to_user(
                    session.group_id,
                    session.user_id,
                    {"type": "device_command", "data": payload},
                )
            case _:
                await session.send_error(
                    ErrorCode.INVALID_MESSAGE,
                    f"Unknown event: {event_type}",
                )

    async def handle_legacy(self, session: Any, data: dict) -> None:
        msg_type = data.get("type")

        if msg_type == "device_command":
            await ws_manager.send_to_user(
                session.group_id,
                session.user_id,
                {"type": "device_command", "data": data.get("data", {})},
            )
        elif msg_type == "ping":
            ws = (
                ws_manager._connections.get(session.group_id, {})
                .get(session.user_id, {})
                .get(session.device_id)
            )
            if ws:
                await ws.send_json({"type": "pong"})
        else:
            logger.warning("[WS-Legacy] Unknown message type: %s", msg_type)


def create_session(
    websocket: WebSocket,
    group_id: str,
    user_id: str,
    device_id: str,
    agent,
    db_factory,
    user_lang: str = "en",
    device_model: str = "",
) -> Session:
    """创建带有 Robot 业务注入的 Session 实例。"""
    return Session(
        websocket=websocket,
        group_id=group_id,
        user_id=user_id,
        device_id=device_id,
        llm_streamer=RobotLLMStreamer(agent),
        agent_state_provider=RobotAgentStateProvider(db_factory, user_lang=user_lang, device_model=device_model),
        memory_factory=RobotMemoryFactory(),
        business_handler=RobotBusinessHandler(),
    )

import logging
import re

from langchain_core.messages import AIMessage, HumanMessage

from robot.config import settings
from robot.memory.redis_client import ChatMemoryClient

logger = logging.getLogger(__name__)


# Each create_game / create_widget tool returns a unique, single-use id
# (e.g. "Game ID: ai_1777826123_a1b2c3" / "Widget ID: aw_..."). When the user
# asks "再做一个游戏 / 再来一个组件" in a follow-up turn, the agent looking at
# chat history sees a prior reply containing the id and incorrectly assumes
# the request was already fulfilled — refusing to call the tool again.
#
# Strip the id phrasing on save (and again defensively on load) so historical
# turns no longer carry "proof of completion" the agent can latch onto.
# Mirrors homehub's `_sanitize_bind_links` (telegram/whatsapp bind URLs are
# single-use and time-expiring; same problem class).
_DELIVERED_PATTERNS = [
    (
        re.compile(r"(?:Game ID|游戏ID)[:：]\s*ai_\d+_[a-zA-Z0-9]+", re.IGNORECASE),
        "[Previous game artifact already delivered to user. To create a new "
        "game, call create_game again — do NOT reuse the prior game_id.]",
    ),
    (
        re.compile(r"(?:Widget ID|小?组件ID)[:：]\s*aw_\d+_[a-zA-Z0-9]+", re.IGNORECASE),
        "[Previous widget artifact already delivered to user. To create a new "
        "widget, call create_widget again — do NOT reuse the prior widget_id.]",
    ),
]


def _sanitize_creation_artifacts(text: str) -> str:
    """Strip single-use game_id / widget_id mentions from text before storing
    in chat history. See _DELIVERED_PATTERNS comment for rationale."""
    if not text:
        return text
    for pattern, replacement in _DELIVERED_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class MemoryManager:
    """Short-term memory manager using Redis sliding window."""

    def __init__(self, user_id: str):
        self.redis = ChatMemoryClient(user_id)
        self.max_rounds = settings.chat_history_max_rounds

    async def save_turn(self, human_msg: str, ai_msg: str) -> None:
        await self.redis.save_turn(human_msg, _sanitize_creation_artifacts(ai_msg))

    async def load_history_as_messages(self) -> list[HumanMessage | AIMessage]:
        """Load recent turns as LangChain message objects."""
        turns = await self.redis.get_recent(self.max_rounds)
        messages = []
        for turn in turns:
            messages.append(HumanMessage(content=turn["human"]))
            messages.append(AIMessage(content=_sanitize_creation_artifacts(turn["ai"])))
        return messages

    async def clear(self) -> None:
        await self.redis.clear()

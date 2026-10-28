import json
import logging
from datetime import datetime

from robot.db.redis import redis_client

logger = logging.getLogger(__name__)


_TTL_SECONDS = 7 * 24 * 3600  # 7 days


class ChatMemoryClient:
    """Redis short-term conversation memory, isolated by user_id."""

    def __init__(self, user_id: str):
        self.user_id = user_id

    @property
    def _messages_key(self) -> str:
        return f"robot:chat:{self.user_id}"

    async def save_turn(self, human_msg: str, ai_msg: str) -> None:
        turn = {
            "human": human_msg,
            "ai": ai_msg,
            "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
        }
        try:
            await redis_client.rpush(
                self._messages_key, json.dumps(turn, ensure_ascii=False)
            )
            await redis_client.expire(self._messages_key, _TTL_SECONDS)
        except Exception as e:
            logger.error("save_turn failed: %s", e)

    async def get_recent(self, limit: int = 20) -> list[dict]:
        try:
            raw = await redis_client.lrange(self._messages_key, -limit, -1)
            return [json.loads(item) for item in raw]
        except Exception as e:
            logger.error("get_recent failed: %s", e)
            return []

    async def get_total_turns(self) -> int:
        try:
            return await redis_client.llen(self._messages_key)
        except Exception:
            return 0

    async def clear(self) -> None:
        try:
            await redis_client.delete(self._messages_key)
        except Exception as e:
            logger.error("clear failed: %s", e)

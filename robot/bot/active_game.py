"""Per-requester 'active game' tracking for the bot.

Lets a vague follow-up ("make the bananas faster") modify the game the user
just created/modified, instead of being mis-read as a new game. Keyed by the
chat-platform user id (Discord user), with a TTL so a stale session naturally
expires into 'no active game' (next message → create).
"""

from __future__ import annotations

import logging

from robot.db.redis import redis_client

logger = logging.getLogger(__name__)

_TTL_SECONDS = 2 * 3600  # active-game session window


def _key(requester_key: str) -> str:
    return f"bot:active_game:{requester_key}"


async def get_active(requester_key: str) -> str | None:
    """Return the user's current game_id, or None if none/expired."""
    try:
        val = await redis_client.get(_key(requester_key))
        return val or None
    except Exception as e:  # pragma: no cover - redis optional
        logger.warning("[bot] get_active failed: %s", e)
        return None


async def set_active(requester_key: str, game_id: str) -> None:
    """Mark game_id as the user's current game (refreshes the TTL)."""
    if not game_id:
        return
    try:
        await redis_client.set(_key(requester_key), game_id, ex=_TTL_SECONDS)
    except Exception as e:  # pragma: no cover - redis optional
        logger.warning("[bot] set_active failed: %s", e)


async def clear_active(requester_key: str) -> None:
    try:
        await redis_client.delete(_key(requester_key))
    except Exception as e:  # pragma: no cover
        logger.warning("[bot] clear_active failed: %s", e)

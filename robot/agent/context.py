"""Agent state building — simplified for single-user robot mode."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from robot.config import settings
from robot.db.models import User
from robot.i18n import normalize_locale

logger = logging.getLogger(__name__)


async def build_agent_state(
    db_factory: async_sessionmaker,
    group_id: str,
    user_id: str,
    device_id: str = "robot",
    user_lang: str = "en",
    device_model: str = "",
) -> dict:
    """Build the agent state dict for a single user."""
    user_lang = normalize_locale(user_lang)
    logger.info("[Context] building state: user=%s device=%s lang=%s model=%s", user_id, device_id, user_lang, device_model)

    user_tz = settings.default_timezone
    async with db_factory() as db:
        result = await db.execute(select(User.timezone).where(User.id == user_id))
        row = result.first()
        if row and row.timezone:
            user_tz = row.timezone

    return {
        "messages": [],
        "owner_id": group_id,
        "user_id": user_id,
        "device_id": device_id,
        "display_name": "",
        "user_tz": user_tz,
        "user_lang": user_lang,
        "game_id": "",
        "device_model": device_model,
    }

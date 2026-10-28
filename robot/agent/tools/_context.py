"""Per-request context and shared dependencies for agent tools."""

import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from robot.config import settings
from robot.i18n import normalize_locale

logger = logging.getLogger(__name__)

# These will be injected at agent creation time
_db_session_factory = None
_ws_manager = None

# Per-request context (set before each agent run)
_current_owner_id: ContextVar[str] = ContextVar("owner_id", default="")
_current_user_id: ContextVar[str] = ContextVar("user_id", default="")
_current_member_role: ContextVar[str] = ContextVar("member_role", default="")
_current_user_tz: ContextVar[str] = ContextVar("user_tz", default=settings.default_timezone)
_current_user_lang: ContextVar[str] = ContextVar("user_lang", default="en")
_current_game_id: ContextVar[str] = ContextVar("game_id", default="")
_current_device_model: ContextVar[str] = ContextVar("device_model", default="")
# 取消检测 callback —— streaming.py 在跑 fast path 工具前 set，工具内部
# （如 create_game）在写 DB / 发 game_ready 前 check：True 表示 Turn 已被
# cancel（用户主动 stopListening / WS 断连 / app kill 后 session_closed），
# 应当走清理路径（删 game_dir + zip）并跳过 DB 入表，避免残留草稿。
_current_cancel_check: ContextVar[Callable[[], bool] | None] = ContextVar(
    "cancel_check", default=None
)


def set_dependencies(db_session_factory, ws_manager):
    global _db_session_factory, _ws_manager
    _db_session_factory = db_session_factory
    _ws_manager = ws_manager


def set_request_context(owner_id: str, user_id: str, member_role: str = "", user_tz: str = "", user_lang: str = "", device_model: str = ""):
    """Set per-request context before running the agent."""
    logger.info("[Tools] set_request_context: owner=%s user=%s role=%s tz=%s lang=%s model=%s", owner_id, user_id, member_role, user_tz, user_lang, device_model)
    _current_owner_id.set(owner_id)
    _current_user_id.set(user_id)
    _current_member_role.set(member_role)
    _current_user_tz.set(user_tz or settings.default_timezone)
    _current_user_lang.set(normalize_locale(user_lang))
    _current_device_model.set(device_model)


def get_user_lang() -> str:
    """Get the user's language from per-request context."""
    return _current_user_lang.get()


def get_current_game_id() -> str:
    """Get the current active game ID from per-request context."""
    return _current_game_id.get()


def _user_tz() -> ZoneInfo:
    """Get the user's timezone from per-request context."""
    return ZoneInfo(_current_user_tz.get())


def _local_to_utc(dt: datetime) -> datetime:
    """Treat naive datetime as user-local, convert to UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_user_tz())
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_now() -> datetime:
    """Current time in UTC (naive)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

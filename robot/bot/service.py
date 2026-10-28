"""Bot service helpers: play-link building, reply formatting, bundle waiting.

Intent (create vs modify vs chat) is now decided by the slim game agent
(robot/bot/agent.py), not a hand-rolled parser — so the old parse_command /
handle_message bridge was removed. These remaining helpers are the
side-effect-free / IO bits the Discord handler still needs.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def load_game_card(game_id: str, *, zh: bool = True) -> dict:
    """Read games/{game_id}/meta.json into display fields for a Discord embed.

    Returns ``{"title", "description", "icon"}``, picking the right language and
    falling back gracefully (legacy single-lang fields, then the id / defaults)
    so the embed always renders even for older or partial meta.
    """
    lang = "zh-Hans" if zh else "en"
    title = description = ""
    icon = "🎮"
    try:
        from robot.games.web_export import GAMES_DIR

        meta = json.loads((GAMES_DIR / game_id / "meta.json").read_text(encoding="utf-8"))
        t_i18n = meta.get("title_i18n") or {}
        d_i18n = meta.get("description_i18n") or {}
        title = t_i18n.get(lang) or t_i18n.get("en") or meta.get("title") or ""
        description = d_i18n.get(lang) or d_i18n.get("en") or meta.get("description") or ""
        icon = meta.get("icon") or icon
    except Exception as e:  # pragma: no cover - missing/partial meta
        logger.debug("[bot] load_game_card(%s) fallback: %s", game_id, e)
    return {"title": title or game_id, "description": description, "icon": icon}


def build_play_url(game_id: str, *, base_url: str | None = None) -> str:
    """Public play link for a game. ``base_url`` overrides the configured one.

    No request object is available in a bot context, so this relies on
    ``settings.share_public_base_url`` (shared with the share landing pages).
    """
    if base_url is None:
        try:
            from robot.config import settings

            base_url = settings.share_public_base_url or ""
        except Exception:  # pragma: no cover
            base_url = ""
    base = (base_url or "http://localhost:8000").rstrip("/")
    return f"{base}/play/{game_id}/"


# ── reply formatting (bilingual; platform layer renders) ──

def format_error(reason: str, *, zh: bool = True) -> str:
    if zh:
        return f"⚠️ 没能完成：{reason}"
    return f"⚠️ Couldn't finish: {reason}"


# Gating block reasons -> friendly bilingual replies.
_BLOCKED_MSG = {
    "disabled": ("🚧 创作暂时关闭了，稍后再试。",
                 "🚧 Creation is temporarily turned off. Try again later."),
    "empty": ("✍️ 描述一下你想要的游戏吧，例如「做个躲陨石的太空游戏」。",
              "✍️ Tell me what game you want, e.g. 'a space game dodging meteors'."),
    "too_long": ("✂️ 描述太长了，精简到 2000 字以内再发。",
                 "✂️ That's too long — keep it under 2000 characters."),
    "content_blocked": ("🚫 这个请求不合适，换个点子吧。",
                        "🚫 That request isn't allowed. Try a different idea."),
    "quota_exceeded": ("⏳ 今天的创作次数用完啦，明天再来～",
                       "⏳ You've hit today's limit — come back tomorrow."),
}


def format_blocked(reason: str, *, zh: bool = True) -> str:
    zh_msg, en_msg = _BLOCKED_MSG.get(
        reason, ("🚫 暂时无法处理这个请求。", "🚫 Can't process this request right now."))
    return zh_msg if zh else en_msg


async def wait_for_bundle(game_id: str, secs: float) -> bool:
    """Poll for the published web bundle so the /play link works when sent.

    create/modify finalize schedules an async web export+publish; this waits
    (up to ``secs``) for ``web_builds/{game_id}/index.html`` to appear. Returns
    True if ready, False on timeout.
    """
    import asyncio

    from robot.games.web_export import published_dir

    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(0.0, secs)
    while True:
        if (published_dir(game_id) / "index.html").is_file():
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(1.0)

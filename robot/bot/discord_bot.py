"""Discord client for the bot adapter.

Listens for messages in the configured channel, drives the game-generation
slim game agent (``robot.bot.agent.run_game_agent``), surfaces live progress,
and replies with the ``/play`` link.
``discord`` is imported lazily so the rest of the codebase / tests don't
require the dependency installed.

Requires (env, see robot/bot/README.md):
  ROBOT_DISCORD_BOT_ENABLED=1
  ROBOT_DISCORD_BOT_TOKEN=...          (Bot token; MESSAGE CONTENT INTENT on)
  ROBOT_DISCORD_BOT_CHANNEL_ID=...     (optional; restrict to one channel)
  ROBOT_BOT_OWNER_USER_ID=...          (backend User.id the bot creates as)
  ROBOT_SHARE_PUBLIC_BASE_URL=https://your.public.host   (for the link)
"""

from __future__ import annotations

import asyncio
import io
import logging

from robot.bot import service
from robot.config import settings

logger = logging.getLogger(__name__)


def _setup_agent_dependencies(ws_like) -> None:
    """Wire the tool layer's db factory + a ws-like progress bridge.

    Mirrors main.py's lifespan, but passes a BotProgressBridge as ws_manager so
    create_game/modify_game's progress / game_ready events route to the Discord
    handler (live status) instead of being skipped.
    """
    from robot.agent.tools._context import set_dependencies
    from robot.db.database import async_session

    set_dependencies(async_session, ws_like)


def run() -> None:
    """Blocking entry point: connect to Discord and serve until killed."""
    if not settings.discord_bot_enabled:
        raise RuntimeError("discord bot disabled (set ROBOT_DISCORD_BOT_ENABLED=1)")
    if not settings.discord_bot_token:
        raise RuntimeError("missing ROBOT_DISCORD_BOT_TOKEN")
    if not settings.bot_owner_user_id:
        raise RuntimeError(
            "missing ROBOT_BOT_OWNER_USER_ID (the backend User.id the bot creates games as)"
        )

    try:
        import discord  # lazy: only needed when actually running the bot
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("discord.py not installed — `pip install discord.py`") from e

    from robot.bot import gating, tunnel
    from robot.bot.agent import build_game_agent, run_game_agent
    from robot.bot.progress import BotProgressBridge

    progress = BotProgressBridge()       # routes tool progress -> Discord status edits
    _setup_agent_dependencies(progress)  # ws_manager = the bridge

    # Public base URL for /play links + embed thumbnails. If not configured,
    # auto-discover a running ngrok tunnel (so links/cards work without env
    # juggling). Empty → falls back to localhost (no thumbnail).
    public_base = tunnel.discover_public_base_url()
    if public_base and not (settings.share_public_base_url or "").strip():
        settings.share_public_base_url = public_base
    logger.info("[discord] public base url: %s", public_base or "(none — localhost, no thumbnail)")

    game_agent = build_game_agent()  # one shared, stateless graph for all users

    intents = discord.Intents.default()
    intents.message_content = True  # privileged; enable in the Developer Portal
    client = discord.Client(intents=intents)

    channel_filter = (settings.discord_bot_channel_id or "").strip()
    owner_id = settings.bot_owner_user_id

    # Concurrency controls (see plan §multi-user):
    #   - per-user lock: serialize one user's messages (no double-create / races)
    #   - global semaphore: cap simultaneous generations (resource/cost guard)
    user_locks: dict[str, asyncio.Lock] = {}
    sem = asyncio.Semaphore(max(1, settings.bot_max_concurrency))

    def _allowed(message) -> bool:
        # DMs always allowed; in guilds, the configured channel (or any if unset).
        if isinstance(message.channel, discord.DMChannel):
            return True
        return (not channel_filter) or str(message.channel.id) == channel_filter

    @client.event
    async def on_ready() -> None:  # noqa: D401
        logger.info("[discord] logged in as %s", client.user)

    @client.event
    async def on_message(message) -> None:
        if message.author.bot or not _allowed(message):
            return
        text = (message.content or "").strip()
        if not text:
            return

        requester = str(message.author.id)
        lock = user_locks.setdefault(requester, asyncio.Lock())
        if lock.locked():
            try:
                await message.channel.send("⏳ 还在处理你上一条，稍等…")
            except Exception:
                pass

        try:
            async with lock:  # serialize this user's requests
                # Gate (kill-switch / content / per-user daily) BEFORE spending money.
                verdict = await gating.gate(text, requester_key=requester, action="create")
                if not verdict.allowed:
                    await message.channel.send(service.format_blocked(verdict.reason))
                    return
                async with sem:  # global concurrency cap
                    # Live status message: one message we edit as progress arrives
                    # (throttled to avoid Discord's edit rate limit), like the app.
                    status = await message.channel.send("🛠️ 收到，正在处理…")
                    loop = asyncio.get_event_loop()
                    last_edit = {"t": 0.0, "msg": ""}

                    async def _sink(payload: dict) -> None:
                        data = (payload or {}).get("data") or {}
                        if data.get("action") not in ("game_progress",):
                            return
                        msg = str(data.get("message") or "").strip()
                        if not msg or msg == last_edit["msg"]:
                            return
                        now = loop.time()
                        if now - last_edit["t"] < 1.6:  # throttle edits
                            return
                        last_edit["t"], last_edit["msg"] = now, msg
                        try:
                            await status.edit(content=f"🛠️ {msg}")
                        except Exception:
                            pass

                    progress.register(requester, _sink)
                    try:
                        reply, game_id = await run_game_agent(
                            game_agent,
                            requester_key=requester,
                            owner_id=owner_id,
                            text=text,
                            user_lang="zh-Hans",
                            display_name=getattr(message.author, "display_name", ""),
                        )
                    finally:
                        progress.unregister(requester)

                    # A game was created/modified this turn → wait for its web
                    # bundle and show a rich card (Discord can't unfurl a
                    # localhost URL; the embed renders nicely regardless).
                    # Pure chat → just the reply text.
                    if game_id:
                        await status.edit(content="🛠️ 正在导出网页版…")
                        ready = await service.wait_for_bundle(game_id, 180.0)
                        url = service.build_play_url(game_id)
                        card = service.load_game_card(game_id, zh=True)
                        embed = discord.Embed(
                            title=f"{card['icon']} {card['title']}"[:256],
                            description=(card["description"][:300] or None),
                            url=url,
                            color=0x5865F2,
                        )
                        # Thumbnail: render the 1200×630 card IN-PROCESS and UPLOAD
                        # it as an attachment. Don't make Discord fetch a URL —
                        # ngrok-free serves an interstitial HTML page to Discord's
                        # bot UA instead of the PNG, so url-based images break.
                        og_file = None
                        try:
                            from robot.games.share_router import _render_og_image
                            png = await asyncio.to_thread(
                                _render_og_image, card["title"], card["description"], card["icon"])
                            og_file = discord.File(io.BytesIO(png), filename="og.png")
                            embed.set_image(url="attachment://og.png")
                        except Exception:
                            logger.exception("[discord] og-image render failed")
                        play = f"▶️ [在浏览器游玩]({url})（摄像头视觉控制）"
                        if not ready:
                            play += "\n（网页版还在导出，稍等一下就能打开）"
                        embed.add_field(name="​", value=play, inline=False)
                        embed.set_footer(text="Motem · 可分享")
                        if og_file is not None:
                            await status.edit(content=(reply or None), embed=embed, attachments=[og_file])
                        else:
                            await status.edit(content=(reply or None), embed=embed)
                    else:
                        # Final result replaces the status message in place.
                        await status.edit(content=(reply or "🤔")[:1900])
        except Exception as e:  # noqa: BLE001 - never crash the listener
            logger.exception("[discord] handling failed")
            try:
                await message.channel.send(service.format_error(str(e)))
            except Exception:
                pass

    logger.info("[discord] starting client…")
    client.run(settings.discord_bot_token, log_handler=None)

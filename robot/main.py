import json
import logging
import logging.config
import time as _time

from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# 强制冷启动 import，让 claude_code_sdk parse_message tolerance patch 立刻 apply。
# 不依赖 game_tools / widget_tools lazy import 路径 —— 否则首次 [game_create]
# 触发的 import 时机有窗口让 query() 在 patch 装上前就开始处理 SSE。
# 见 robot/agent/tools/_sdk_patch.py。
import robot.agent.tools._sdk_patch  # noqa: F401  side-effect import

from robot.auth.router import router as auth_router
from robot.social.router import router as social_router
from robot.calendar.router import router as calendar_router
from robot.tasks.router import router as tasks_router
from robot.notification.router import router as notification_router
from robot.devices.router import router as devices_router
from robot.chat.router import router as chat_router
from robot.games.router import router as games_router
from robot.games.share_router import router as share_router
from robot.games.play_router import router as play_router
from robot.widgets.router import router as widgets_router
from robot.widgets.data_router import router as widget_data_router
from robot.app_config.router import router as app_config_router
from robot.auth.service import decode_token
from robot.auth.scope import resolve_websocket_group
from robot.weather.service import get_weather as fetch_weather
from aichat.websocket.handlers import (
    handle_legacy_message,
    handle_v4_message,
)
from robot.websocket import ws_manager
from robot.db.database import async_session
from robot.db.models import User

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from robot.config import settings
from robot.rate_limiter import http_limiter
from robot.logging_config import LOGGING_CONFIG
from robot.i18n import normalize_locale, set_locale
from sqlalchemy import select

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)

_agent = None
_db_session_factory = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent, _db_session_factory

    from robot.config import settings
    from aichat.config import AIChatConfig, configure

    configure(AIChatConfig(
        grpc_asr_addr=settings.grpc_asr_addr,
        grpc_tts_addr=settings.grpc_tts_addr,
        speech_asr_model=settings.speech_asr_model,
        speech_asr_sample_rate=settings.speech_asr_sample_rate,
        speech_tts_model=settings.speech_tts_model,
        speech_tts_voice=settings.speech_tts_voice,
        speech_tts_sample_rate=settings.speech_tts_sample_rate,
        speech_opus_enabled=settings.speech_opus_enabled,
        server_version=settings.server_version,
        qwen_api_key=settings.dashscope_api_key,
    ))

    logger.info("Initializing Robot Agent...")
    try:
        from robot.agent.robot_agent import create_robot_agent

        agent = create_robot_agent(async_session, ws_manager)
        _agent = agent
        _db_session_factory = async_session
        logger.info("Robot Agent initialized successfully")

        from robot.chat.router import set_dependencies as set_chat_deps
        set_chat_deps(agent, async_session)
    except Exception as e:
        logger.warning("Robot Agent init failed (LLM deps may be missing): %s", e)

    logger.info("Starting notification scheduler...")
    try:
        from robot.notification.scheduler import start_scheduler

        start_scheduler(async_session, ws_manager)
        logger.info("Notification scheduler started")
    except Exception as e:
        logger.warning("Scheduler init failed: %s", e)

    yield

    try:
        from robot.notification.scheduler import get_scheduler

        scheduler = get_scheduler()
        if scheduler.running:
            scheduler.shutdown()
    except Exception:
        pass

    try:
        from aichat.grpc_clients.asr_channel import RecognitionStream
        from aichat.grpc_clients.tts_channel import SpeechStream

        await RecognitionStream.close_all_channels()
        SpeechStream.close_all_channels()
    except Exception as e:
        logger.warning("gRPC channel cleanup error: %s", e)

    logger.info("Shutting down...")


app = FastAPI(title="MotemBackend", version="0.1.0", lifespan=lifespan)
app.state.limiter = http_limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept-Language",
                   "Accept", "X-Forwarded-For", "X-Forwarded-Proto"],
)


@app.middleware("http")
async def locale_middleware(request: Request, call_next):
    set_locale(request.headers.get("accept-language"))
    return await call_next(request)

# ── Routers ──

app.include_router(auth_router)
app.include_router(social_router)
app.include_router(calendar_router)
app.include_router(tasks_router)
app.include_router(notification_router)
app.include_router(devices_router)
app.include_router(chat_router)
app.include_router(games_router)
app.include_router(share_router)
app.include_router(play_router)
app.include_router(widgets_router)
app.include_router(widget_data_router)
app.include_router(app_config_router)

# ── Static files (H5 games, etc.) ──

_static_dir = Path(__file__).resolve().parent.parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ── Health ──


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Weather ──


@app.get("/api/weather")
@http_limiter.limit("30/minute", exempt_when=lambda: "pytest" in __import__("sys").modules)
async def weather(
    request: Request,
    lat: float | None = Query(default=None),
    lon: float | None = Query(default=None),
):
    # Validate coordinate ranges to prevent using us as a weather proxy
    if lat is not None and not (-90 <= lat <= 90):
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=400, detail="invalid lat")
    if lon is not None and not (-180 <= lon <= 180):
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(status_code=400, detail="invalid lon")
    client_ip = request.headers.get("X-Forwarded-For", "") or (request.client.host if request.client else "")
    return await fetch_weather(lat, lon, client_ip=client_ip)


# ── WebSocket ──


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
    device_id: str = Query(...),
    group_id: str = Query(default=""),
    device_model: str = Query(default=""),
):
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        await websocket.close(code=4001, reason="Invalid token")
        return

    user_id = payload.get("sub")
    if not isinstance(user_id, str) or not user_id.strip():
        await websocket.close(code=4001, reason="Invalid token")
        return
    try:
        group_id = resolve_websocket_group(user_id, group_id)
    except PermissionError:
        await websocket.close(code=4003, reason="Group access denied")
        return
    token_exp: float = float(payload.get("exp", 0))

    await ws_manager.connect(websocket, group_id, user_id, device_id)

    # Extract preferred language from Accept-Language header.
    user_lang = normalize_locale(websocket.headers.get("accept-language"))
    set_locale(user_lang)

    session_factory = _db_session_factory or async_session
    async with session_factory() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user and user.locale != user_lang:
            user.locale = user_lang
            await db.commit()

    from robot.integration import create_session

    session = create_session(
        websocket=websocket,
        group_id=group_id,
        user_id=user_id,
        device_id=device_id,
        agent=_agent,
        db_factory=_db_session_factory,
        user_lang=user_lang,
        device_model=device_model,
    )

    try:
        while True:
            message = await websocket.receive()
            msg_type = message.get("type", "")

            if msg_type == "websocket.disconnect":
                break

            # 消息大小防护：超过 1 MB 的消息直接丢弃，防 OOM
            _raw_text = message.get("text") or ""
            _raw_bytes = message.get("bytes") or b""
            if len(_raw_text) > 1_000_000 or len(_raw_bytes) > 1_000_000:
                logger.warning("[WS] oversized message from %s (%d bytes), dropping",
                               user_id, max(len(_raw_text), len(_raw_bytes)))
                continue

            # 每条消息前校验 token 是否已到期
            if _time.time() > token_exp:
                logger.info("[WS] token expired for user %s, closing", user_id)
                await websocket.close(code=4001, reason="Token expired")
                break

            if msg_type != "websocket.receive":
                continue

            raw_bytes = message.get("bytes")
            if raw_bytes:
                await handle_v4_message(session, raw_bytes)
                continue

            text = message.get("text")
            if not text:
                continue

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from %s: %s", user_id, text[:100])
                continue

            event_type = data.get("type", "")
            if "." in event_type:
                await handle_v4_message(session, text)
            else:
                await handle_legacy_message(session, data)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("WebSocket error: %s", e, exc_info=True)
    finally:
        await session.close()
        ws_manager.disconnect(group_id, user_id, device_id)

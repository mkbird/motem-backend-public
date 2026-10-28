"""Per-user widget data store — fetched by AI widgets via authenticated REST.

Channels currently supported:
- `weather`: latest WeatherResponse cached by `POST /weather/refresh?lat&lon`,
  read by `GET /weather`.

Adding a new channel: define it in WIDGET_DATA_CHANNELS, add a refresh
endpoint that calls the appropriate upstream service and writes via
`_upsert_channel`.

The intent is for AI-generated widgets to NOT depend on the iOS
`window.motem.data` bridge (which keeps them platform-bound and
breaks when the bridge contract changes). Instead they `fetch()` the
backend; backend is the single source of truth.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.auth.dependencies import get_current_user
from robot.db.database import get_db
from robot.db.models import User, UserWidgetData
from robot.weather.service import get_weather

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/widget-data", tags=["widget-data"])

# Whitelist of channels iOS / widgets are allowed to read/write. Keeps
# attackers from stuffing arbitrary keys into the table.
WIDGET_DATA_CHANNELS = {"weather", "todos", "pomodoro"}


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


async def _read_channel(db: AsyncSession, user_id: str, channel: str) -> dict | None:
    row = (
        await db.execute(
            select(UserWidgetData).where(
                UserWidgetData.user_id == user_id,
                UserWidgetData.channel == channel,
            )
        )
    ).scalar_one_or_none()
    if not row:
        return None
    try:
        return json.loads(row.payload)
    except Exception as e:
        logger.warning("[WidgetData] decode failed user=%s channel=%s: %s",
                       user_id, channel, e)
        return None


async def _upsert_channel(db: AsyncSession, user_id: str, channel: str, payload: dict) -> None:
    if channel not in WIDGET_DATA_CHANNELS:
        raise HTTPException(status_code=400, detail=f"unknown channel: {channel}")

    # MySQL atomic upsert (INSERT ... ON DUPLICATE KEY UPDATE) — 之前用的
    # SELECT-then-INSERT/UPDATE 模式，两个 refresh 几乎同时到达时都看到
    # 空行 → 都 INSERT → 第二次撞 PRIMARY KEY 抛 IntegrityError 把整个
    # request 干挂掉。原子 upsert 一条 SQL 解决。
    from sqlalchemy.dialects.mysql import insert as mysql_insert

    payload_json = json.dumps(payload, ensure_ascii=False)
    now = datetime.utcnow()
    stmt = mysql_insert(UserWidgetData).values(
        user_id=user_id, channel=channel, payload=payload_json, updated_at=now,
    )
    stmt = stmt.on_duplicate_key_update(payload=payload_json, updated_at=now)
    await db.execute(stmt)
    await db.commit()


# ─────────────────────────────────────────────────────────────────────
# Generic GET — read any whitelisted channel
# ─────────────────────────────────────────────────────────────────────


@router.get("/{channel}")
async def read_widget_data(
    channel: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the latest cached payload for `channel` belonging to current user.

    Response: `{channel, payload, updated_at}` or 404 if never written.
    """
    if channel not in WIDGET_DATA_CHANNELS:
        raise HTTPException(status_code=400, detail=f"unknown channel: {channel}")

    row = (
        await db.execute(
            select(UserWidgetData).where(
                UserWidgetData.user_id == current_user.id,
                UserWidgetData.channel == channel,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no {channel} data for user")
    try:
        payload = json.loads(row.payload)
    except Exception:
        payload = {}
    return {
        "channel": channel,
        "payload": payload,
        "updated_at": row.updated_at.isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────
# Weather — refresh = fetch upstream + cache
# ─────────────────────────────────────────────────────────────────────


@router.post("/weather/refresh")
async def refresh_weather(
    request: Request,
    lat: float | None = Query(default=None),
    lon: float | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """iOS calls this when its WeatherLocationHelper has a fresh location.
    Backend hits the upstream weather provider, caches the result keyed by
    user_id, and returns the same WeatherResponse the iOS card uses.
    """
    client_ip = request.headers.get("X-Forwarded-For", "") or (
        request.client.host if request.client else "")
    weather = await get_weather(lat, lon, client_ip=client_ip)
    if weather is None:
        raise HTTPException(status_code=502, detail="weather provider unavailable")

    payload = weather if isinstance(weather, dict) else dict(weather)
    # 不要把 error 结果覆盖到 cache 里 —— iOS 同一时间往往发两条 refresh
    # （一条带 lat/lon、一条 IP-fallback 无坐标），无坐标那条经常 IP
    # geolocation 失败返回 {"error": "无法获取位置"}。如果就这么写进 cache，
    # 会把上一秒成功的 {temp, city, ...} 覆盖成错误，widget 拿到的全是 "--"。
    # 错误时返回原 error 给 caller，但保留 cache 上一份好的。
    if not payload.get("error") and ("temp" in payload or "city" in payload):
        await _upsert_channel(db, current_user.id, "weather", payload)
        logger.info("[WidgetData] cached weather for user=%s lat=%s lon=%s city=%s",
                    current_user.id, lat, lon, payload.get("city"))
    else:
        logger.info("[WidgetData] skip cache (error/empty) user=%s lat=%s lon=%s payload=%s",
                    current_user.id, lat, lon, payload)
    return weather

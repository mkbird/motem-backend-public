"""APNs push notification service.

Token-based JWT auth（.p8 key），同一份 key 同时覆盖 sandbox + production，
env 由 `user_devices.apns_environment` 决定。aioapns 在每个 (env, topic)
对应一个 long-lived `APNs` 实例（内部维护 HTTP/2 连接 + 自动 JWT 刷新）。

设置不齐时（没配 .p8 / key_id / team_id / bundle_id）退化成 log-only 兜底，
不影响其它功能。
"""

import logging
import os
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.config import settings
from robot.db.models import UserDevice

logger = logging.getLogger(__name__)

try:
    from aioapns import APNs, NotificationRequest, PushType  # type: ignore

    _AIOAPNS_AVAILABLE = True
except ImportError:  # pragma: no cover
    APNs = None  # type: ignore
    NotificationRequest = None  # type: ignore
    PushType = None  # type: ignore
    _AIOAPNS_AVAILABLE = False


# {(env, topic): APNs}
_clients: dict[tuple[str, str], "APNs"] = {}


def _is_configured() -> bool:
    return bool(
        _AIOAPNS_AVAILABLE
        and settings.apns_key_path
        and os.path.exists(settings.apns_key_path)
        and settings.apns_key_id
        and settings.apns_team_id
        and settings.apns_bundle_id
    )


def _get_client(env: str) -> Optional["APNs"]:
    """Lazily build an APNs client for the requested environment.

    `env` ∈ {"sandbox", "production"}. Returns None if APNs is not configured
    (callers should fall back to logging).
    """
    if not _is_configured():
        return None
    use_sandbox = env != "production"
    topic = settings.apns_bundle_id
    key = (env, topic)
    client = _clients.get(key)
    if client is None:
        client = APNs(
            key=settings.apns_key_path,
            key_id=settings.apns_key_id,
            team_id=settings.apns_team_id,
            topic=topic,
            use_sandbox=use_sandbox,
        )
        _clients[key] = client
        logger.info(
            "[APNs] client built env=%s topic=%s use_sandbox=%s",
            env,
            topic,
            use_sandbox,
        )
    return client


# Token validity outcomes that mean the device should be forgotten.
# aioapns surfaces these as `response.description` strings.
_DEAD_TOKEN_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


async def _purge_dead_token(db: AsyncSession, device: UserDevice) -> None:
    device.apns_token = None
    device.apns_environment = None
    await db.commit()
    logger.info(
        "[APNs] purged dead token user=%s device=%s", device.user_id, device.device_id
    )


async def _fetch_user_devices(db: AsyncSession, user_id: str) -> list[UserDevice]:
    result = await db.execute(
        select(UserDevice).where(
            UserDevice.user_id == user_id,
            UserDevice.apns_token.isnot(None),
        )
    )
    return list(result.scalars().all())


async def get_user_device_tokens(db: AsyncSession, user_id: str) -> list[str]:
    """Legacy helper kept for tests. Returns just the token strings."""
    devices = await _fetch_user_devices(db, user_id)
    return [d.apns_token for d in devices if d.apns_token]


async def _send_one(
    db: AsyncSession,
    device: UserDevice,
    payload: dict,
    push_type: str = "alert",
) -> bool:
    env = device.apns_environment or "sandbox"
    client = _get_client(env)
    if client is None:
        logger.info(
            "[APNs] log-only (not configured) → token=%s...%s env=%s payload_keys=%s",
            (device.apns_token or "")[:8],
            (device.apns_token or "")[-4:],
            env,
            list(payload.keys()),
        )
        return False

    pt = PushType.ALERT if push_type == "alert" else PushType.BACKGROUND
    request = NotificationRequest(
        device_token=device.apns_token,
        message=payload,
        push_type=pt,
    )
    try:
        response = await client.send_notification(request)
    except Exception as exc:  # pragma: no cover — network/HTTP2 errors
        logger.warning(
            "[APNs] send failed user=%s env=%s err=%s", device.user_id, env, exc
        )
        return False

    if response.is_successful:
        return True

    desc = getattr(response, "description", "") or ""
    status = getattr(response, "status", "")
    logger.warning(
        "[APNs] non-success user=%s env=%s status=%s desc=%s",
        device.user_id,
        env,
        status,
        desc,
    )
    if desc in _DEAD_TOKEN_REASONS or status == "410":
        try:
            await _purge_dead_token(db, device)
        except Exception:  # pragma: no cover
            logger.exception("[APNs] failed to purge dead token")
    return False


async def send_push(
    db: AsyncSession,
    user_id: str,
    title: str,
    body: str,
    category: str = "system",
    data: dict | None = None,
) -> bool:
    """Send an alert push to every device registered for `user_id`.

    Returns True if at least one device was successfully delivered to (or
    successfully log-only-handled when APNs creds are absent).
    """
    devices = await _fetch_user_devices(db, user_id)
    if not devices:
        logger.info("[APNs] no devices for user=%s, skipping push", user_id)
        return False

    payload = {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default",
            "category": category,
        },
    }
    if data:
        payload["data"] = data

    delivered = 0
    for device in devices:
        ok = await _send_one(db, device, payload, push_type="alert")
        if ok:
            delivered += 1

    # When APNs is not configured we still want callers to consider the
    # notification "fired" for the legacy log-only flow.
    return delivered > 0 or not _is_configured()


async def send_silent_push(
    db: AsyncSession,
    user_id: str,
    data: dict,
) -> bool:
    """Send a content-available silent push (background data refresh)."""
    devices = await _fetch_user_devices(db, user_id)
    if not devices:
        return False

    payload = {
        "aps": {"content-available": 1},
        "data": data,
    }
    delivered = 0
    for device in devices:
        ok = await _send_one(db, device, payload, push_type="background")
        if ok:
            delivered += 1
    return delivered > 0 or not _is_configured()


def _reset_clients_for_tests() -> None:
    """Drop cached APNs clients — tests reconfigure env vars between cases."""
    _clients.clear()

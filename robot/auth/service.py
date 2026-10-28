from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.config import settings
from robot.db.models import (
    AIWidget,
    CalendarEvent,
    DeviceRegistry,
    EventParticipant,
    GameLike,
    GameStats,
    NotificationRecord,
    PendingConfirmation,
    Task,
    User,
    UserDevice,
    UserFollow,
)

logger = logging.getLogger(__name__)

_GAMES_DIR = Path(__file__).resolve().parent.parent.parent / "games"
_WIDGETS_DIR = Path(__file__).resolve().parent.parent.parent / "widgets"

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
# 预计算 dummy hash 防止每次为不存在用户生成 hash（bcrypt ~250ms 会阻塞 asyncio）
_DUMMY_HASH: str = pwd_context.hash("dummy-timing-sentinel")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(user_id: str, owner_id: str | None = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_access_token_expire_minutes
    )
    payload = {"sub": user_id, "exp": expire, "type": "access"}
    if owner_id:
        payload["owner_id"] = owner_id
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str) -> str:
    import uuid
    expire = datetime.now(timezone.utc) + timedelta(
        days=settings.jwt_refresh_token_expire_days
    )
    payload = {
        "sub": user_id,
        "exp": expire,
        "type": "refresh",
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


async def revoke_refresh_jti(jti: str, ttl_seconds: int) -> None:
    """将 refresh token 的 jti 写入 Redis 黑名单，TTL = token 剩余有效期。"""
    try:
        from robot.db.redis import redis_client
        await redis_client.setex(f"revoked_jti:{jti}", ttl_seconds, "1")
    except Exception as e:
        logger.warning("[Auth] revoke_refresh_jti failed (non-fatal): %s", e)


async def is_refresh_jti_revoked(jti: str) -> bool:
    """检查 jti 是否在黑名单中（已撤销或已轮换）。Redis 不可用时放行（降级）。"""
    try:
        from robot.db.redis import redis_client
        return await redis_client.exists(f"revoked_jti:{jti}") > 0
    except Exception as e:
        logger.warning("[Auth] is_refresh_jti_revoked failed, allowing: %s", e)
        return False


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
    except JWTError:
        return None


async def register_user(
    db: AsyncSession, email: str, password: str, display_name: str, phone: str | None
) -> User:
    user = User(
        email=email,
        password_hash=hash_password(password),
        display_name=display_name,
        phone=phone,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User | None:
    import asyncio
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        # Run dummy bcrypt in thread to prevent timing-based user enumeration
        # without blocking the asyncio event loop
        await asyncio.to_thread(verify_password, "dummy-sentinel", _DUMMY_HASH)
        return None
    if await asyncio.to_thread(verify_password, password, user.password_hash):
        return user
    return None


async def get_user_by_id(db: AsyncSession, user_id: str) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


def _collect_user_ai_game_ids(user_id: str) -> list[str]:
    """扫描 games 目录，找出所有 owner_id == user_id 的 AI 游戏。
    只读，不删文件 — 在 DB 事务 commit 后才执行删除。"""
    ids: list[str] = []
    if not _GAMES_DIR.is_dir():
        return ids
    for d in _GAMES_DIR.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue
        meta_file = d / "meta.json"
        if not meta_file.exists():
            continue
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            if data.get("owner_id") == user_id:
                ids.append(d.name)
        except Exception as e:
            logger.warning("[delete_account] meta read failed for %s: %s", d.name, e)
    return ids


def _delete_ai_game_files(game_ids: list[str]) -> None:
    """物理删除游戏目录 + zip + sibling .claude 会话存储。
    在 DB commit 之后调用，commit 失败不会留孤儿文件。"""
    from robot.games._session_paths import wipe_game_artifacts
    for gid in game_ids:
        try:
            wipe_game_artifacts(_GAMES_DIR / gid)
        except Exception as e:
            logger.warning("[delete_account] wipe failed %s: %s", gid, e)


def _delete_ai_widget_files(widget_ids: list[str]) -> None:
    """物理删除 widget 目录 + zip。和 _delete_ai_game_files 同样的事务后置策略。"""
    for wid in widget_ids:
        widget_dir = _WIDGETS_DIR / wid
        zip_path = _WIDGETS_DIR / f"{wid}.zip"
        if widget_dir.is_dir():
            shutil.rmtree(widget_dir, ignore_errors=True)
        if zip_path.exists():
            try:
                zip_path.unlink()
            except Exception as e:
                logger.warning("[delete_account] widget zip unlink failed %s: %s", wid, e)


async def delete_account(db: AsyncSession, user: User) -> None:
    """硬删除当前用户的全部数据：所有 FK 指向该用户的 DB 行 + 用户拥有的 AI 游戏。

    顺序：先收集要删的游戏 ids（不删文件）→ DB 子表 → DB 父表 → User 行 →
    db.commit() → commit 成功后才删文件，避免事务回滚但文件已删的脏状态。
    """
    user_id = user.id
    ai_game_ids = _collect_user_ai_game_ids(user_id)

    # AI widgets — DB 行通过 FK CASCADE 会跟着 user 删，但物理文件需要单独清理。
    widget_rows = (
        await db.execute(select(AIWidget).where(AIWidget.owner_id == user_id))
    ).scalars().all()
    ai_widget_ids = [row.widget_id for row in widget_rows]

    # GameStats 跟着 AI 游戏一起删（无 FK，按 game_id 字符串匹配）
    if ai_game_ids:
        await db.execute(delete(GameStats).where(GameStats.game_id.in_(ai_game_ids)))
        await db.execute(delete(GameLike).where(GameLike.game_id.in_(ai_game_ids)))

    # 该用户对其他人游戏的点赞
    await db.execute(delete(GameLike).where(GameLike.user_id == user_id))

    # 关注关系（双向）。迁移层也设置了 ON DELETE CASCADE；这里显式清理，
    # 保持与现有 delete_account 对其他子表的行为一致。
    await db.execute(
        delete(UserFollow).where(
            or_(
                UserFollow.follower_id == user_id,
                UserFollow.followee_id == user_id,
            )
        )
    )

    # 通知（owner 或 target 是该用户）
    await db.execute(
        delete(NotificationRecord).where(
            or_(
                NotificationRecord.owner_id == user_id,
                NotificationRecord.target_user_id == user_id,
            )
        )
    )

    # 待确认（双向）
    await db.execute(
        delete(PendingConfirmation).where(
            or_(
                PendingConfirmation.from_user_id == user_id,
                PendingConfirmation.to_user_id == user_id,
            )
        )
    )

    # 该用户作为参与者的事件参与记录
    await db.execute(
        delete(EventParticipant).where(EventParticipant.user_id == user_id)
    )

    # 日历事件（owner_id 或 created_by 是该用户）—— 先删事件会级联其
    # event_participants（cascade="all, delete-orphan"，见 models.py）
    await db.execute(
        delete(CalendarEvent).where(
            or_(
                CalendarEvent.owner_id == user_id,
                CalendarEvent.created_by == user_id,
            )
        )
    )

    # 任务（owner / created_by / assigned_to 任一）
    await db.execute(
        delete(Task).where(
            or_(
                Task.owner_id == user_id,
                Task.created_by == user_id,
                Task.assigned_to == user_id,
            )
        )
    )

    # 智能家居 / 注册的设备
    await db.execute(delete(DeviceRegistry).where(DeviceRegistry.owner_id == user_id))
    await db.execute(delete(UserDevice).where(UserDevice.user_id == user_id))

    # 最后删 User 行
    await db.delete(user)
    await db.commit()

    # commit 成功 → 物理删游戏 / widget 文件
    _delete_ai_game_files(ai_game_ids)
    _delete_ai_widget_files(ai_widget_ids)
    logger.info("[delete_account] user=%s ai_games=%d ai_widgets=%d",
                user_id, len(ai_game_ids), len(ai_widget_ids))


async def register_device(
    db: AsyncSession,
    user_id: str,
    device_id: str,
    device_name: str | None = None,
    apns_token: str | None = None,
    apns_environment: str | None = None,
) -> UserDevice:
    result = await db.execute(
        select(UserDevice).where(
            UserDevice.user_id == user_id, UserDevice.device_id == device_id
        )
    )
    device = result.scalar_one_or_none()
    if device:
        device.last_seen = datetime.now(timezone.utc).replace(tzinfo=None)
        if device_name:
            device.device_name = device_name
        if apns_token is not None:
            device.apns_token = apns_token
        if apns_environment is not None:
            device.apns_environment = apns_environment
    else:
        device = UserDevice(
            user_id=user_id,
            device_id=device_id,
            device_name=device_name,
            apns_token=apns_token,
            apns_environment=apns_environment,
        )
        db.add(device)
    await db.commit()
    return device

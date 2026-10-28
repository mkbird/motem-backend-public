import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from robot.auth.dependencies import get_current_user
from robot.auth.models import (
    APNsTokenRequest,
    AvatarUploadResponse,
    RefreshTokenRequest,
    TokenResponse,
    UpdateUserRequest,
    UserLoginRequest,
    UserRegisterRequest,
    UserResponse,
)
from robot.auth.service import (
    authenticate_user,
    create_access_token,
    create_refresh_token,
    decode_token,
    delete_account,
    is_refresh_jti_revoked,
    register_device,
    register_user,
    revoke_refresh_jti,
)
from robot.config import settings
from robot.db.database import get_db
from robot.db.models import User, UserFollow
from robot.i18n import _, get_locale
from robot.rate_limiter import http_limiter

USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,19}$")


def _avatar_dir() -> Path:
    path = Path(settings.avatar_dir)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent.parent / path
    return path


async def _user_with_counts(db: AsyncSession, user: User) -> dict:
    """返回 user dict + followers_count / following_count（pydantic 用 model_validate）。"""
    followers_q = select(func.count(UserFollow.id)).where(UserFollow.followee_id == user.id)
    following_q = select(func.count(UserFollow.id)).where(UserFollow.follower_id == user.id)
    followers = (await db.execute(followers_q)).scalar() or 0
    following = (await db.execute(following_q)).scalar() or 0
    return {
        "id": user.id,
        "email": user.email,
        "phone": user.phone,
        "display_name": user.display_name,
        "timezone": user.timezone,
        "avatar_url": user.avatar_url,
        "username": user.username,
        "bio": user.bio,
        "followers_count": followers,
        "following_count": following,
    }

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _is_test() -> bool:
    """pytest 中跳过速率限制，避免测试套件把自己打到 429。"""
    import sys
    return "pytest" in sys.modules


@router.post("/register", response_model=UserResponse, status_code=201)
@http_limiter.limit("5/minute", exempt_when=_is_test)
async def register(request: Request, req: UserRegisterRequest, db: AsyncSession = Depends(get_db)):
    try:
        user = await register_user(db, req.email, req.password, req.display_name, req.phone)
    except Exception:
        raise HTTPException(status_code=400, detail=_("EMAIL_OR_PHONE_REGISTERED"))
    user.locale = get_locale()
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
@http_limiter.limit("10/minute", exempt_when=_is_test)
async def login(request: Request, req: UserLoginRequest, db: AsyncSession = Depends(get_db)):
    user = await authenticate_user(db, req.email, req.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_("INVALID_CREDENTIALS")
        )
    current_locale = get_locale()
    if getattr(user, "locale", None) != current_locale:
        user.locale = current_locale
        await db.commit()
    await register_device(db, user.id, req.device_id, req.device_name)
    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(req: RefreshTokenRequest):
    payload = decode_token(req.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail=_("INVALID_REFRESH_TOKEN"))

    jti = payload.get("jti")
    user_id = payload["sub"]

    # Rotation: check if this jti is already revoked (reuse attack or already rotated)
    if jti and await is_refresh_jti_revoked(jti):
        raise HTTPException(status_code=401, detail=_("INVALID_REFRESH_TOKEN"))

    # Revoke current jti so it cannot be reused
    if jti:
        import time as _time
        exp = payload.get("exp", 0)
        ttl = max(1, int(exp - _time.time()))
        await revoke_refresh_jti(jti, ttl)

    return TokenResponse(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
    )


@router.get("/me", response_model=UserResponse)
async def me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return UserResponse.model_validate(await _user_with_counts(db, user))


@router.patch("/me", response_model=UserResponse)
async def update_me(
    req: UpdateUserRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if req.timezone is not None:
        user.timezone = req.timezone
    if req.display_name is not None:
        user.display_name = req.display_name
    if req.bio is not None:
        user.bio = req.bio[:280]   # cap defensively
    if req.avatar_url is not None:
        user.avatar_url = req.avatar_url
    if req.username is not None:
        new_handle = req.username.strip().lower()
        if not USERNAME_PATTERN.match(new_handle):
            raise HTTPException(status_code=422, detail=_("USERNAME_INVALID_FORMAT"))
        # 唯一性 —— 大小写不敏感，跳过自己
        existing = await db.execute(
            select(User).where(User.username == new_handle, User.id != user.id)
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail=_("USERNAME_TAKEN"))
        user.username = new_handle
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(await _user_with_counts(db, user))


@router.post("/me/avatar", response_model=AvatarUploadResponse)
async def upload_avatar(
    image: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上传头像 —— 存到 ROBOT_AVATAR_DIR (默认 static/avatars)，返回 URL。
    JPEG / PNG 通过；服务端不做尺寸压缩，client 已经在裁剪页 resize。"""
    # 简单 MIME 校验
    if image.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(status_code=415, detail=_("AVATAR_BAD_TYPE"))
    avatar_dir = _avatar_dir()
    avatar_dir.mkdir(parents=True, exist_ok=True)
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[image.content_type]
    # 加随机后缀避免 CDN / 浏览器 cache 旧图
    filename = f"{user.id}_{uuid.uuid4().hex[:8]}.{ext}"
    target = avatar_dir / filename
    contents = await image.read()
    if len(contents) > 5 * 1024 * 1024:  # 5MB cap
        raise HTTPException(status_code=413, detail=_("AVATAR_TOO_LARGE"))
    target.write_bytes(contents)
    avatar_url = f"/static/avatars/{filename}"
    user.avatar_url = avatar_url
    await db.commit()
    return AvatarUploadResponse(avatar_url=avatar_url)


@router.post("/me/device-token", status_code=204)
async def upload_device_token(
    req: APNsTokenRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """iOS 端 `didRegisterForRemoteNotificationsWithDeviceToken` 后调一次。

    幂等：同 device_id 重复上报会刷新 token + last_seen。环境必须是
    `sandbox` 或 `production`；其它值 422，避免 push 路由到错误集群。
    """
    env = req.environment.lower()
    if env not in {"sandbox", "production"}:
        raise HTTPException(status_code=422, detail=_("APNS_ENV_INVALID"))
    await register_device(
        db,
        user_id=user.id,
        device_id=req.device_id,
        device_name=req.device_name,
        apns_token=req.apns_token,
        apns_environment=env,
    )
    return Response(status_code=204)


@router.delete("/me", status_code=204)
async def delete_me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """硬删除当前账号 — 清空 DB 中所有 FK 指向该用户的行，
    以及用户名下所有 AI 游戏文件。token 之后会因 user 行不存在而 401。"""
    await delete_account(db, user)
    return Response(status_code=204)

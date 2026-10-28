from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from robot.auth.service import decode_token, get_user_by_id
from robot.db.database import get_db
from robot.db.models import User
from robot.i18n import _, normalize_locale

security = HTTPBearer()


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    payload = decode_token(credentials.credentials)
    if not payload or payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_("INVALID_TOKEN")
        )
    user = await get_user_by_id(db, payload["sub"])
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_("USER_NOT_FOUND")
        )
    locale = normalize_locale(request.headers.get("accept-language"))
    if getattr(user, "locale", None) != locale:
        user.locale = locale
        await db.commit()
    return user


# 可选鉴权 — Authorization 头存在则解析返回 User，缺失或无效返回 None。
# 用于公开端点希望"知道访问者是谁但不强制登录"的场景（如 GET stats 想返回 liked_by_me）。
_optional_security = HTTPBearer(auto_error=False)


async def get_current_user_optional(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_security),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    if credentials is None:
        return None
    payload = decode_token(credentials.credentials)
    if not payload or payload.get("type") != "access":
        return None
    user = await get_user_by_id(db, payload["sub"])
    return user

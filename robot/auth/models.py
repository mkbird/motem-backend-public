from pydantic import BaseModel, EmailStr


class UserRegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str
    phone: str | None = None


class UserLoginRequest(BaseModel):
    email: EmailStr
    password: str
    device_id: str
    device_name: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class UpdateUserRequest(BaseModel):
    timezone: str | None = None
    display_name: str | None = None
    username: str | None = None
    bio: str | None = None
    avatar_url: str | None = None


class UserResponse(BaseModel):
    id: str
    email: str
    phone: str | None
    display_name: str
    timezone: str | None
    avatar_url: str | None
    username: str | None = None
    bio: str | None = None
    followers_count: int = 0
    following_count: int = 0

    model_config = {"from_attributes": True}


class OtherProfileResponse(BaseModel):
    """GET /api/users/{id_or_handle} 的响应。比 UserResponse 多 follow 关系字段。"""
    id: str
    username: str | None = None
    display_name: str
    avatar_url: str | None = None
    bio: str | None = None
    followers_count: int = 0
    following_count: int = 0
    is_following: bool = False
    follows_me: bool = False

    model_config = {"from_attributes": True}


class UserMiniResponse(BaseModel):
    """followers / following 列表里每行用的精简视图。"""
    id: str
    username: str | None = None
    display_name: str
    avatar_url: str | None = None
    is_following: bool = False

    model_config = {"from_attributes": True}


class FollowListResponse(BaseModel):
    items: list[UserMiniResponse]
    next_cursor: str | None = None


class FollowToggleResponse(BaseModel):
    is_following: bool
    followers_count: int


class AvatarUploadResponse(BaseModel):
    avatar_url: str


class APNsTokenRequest(BaseModel):
    """iOS 注册成功后把 APNs device token 上报给后端。

    `device_id` 与登录时的 `UserLoginRequest.device_id` 同义（来自
    `identifierForVendor`），用来唯一标定 user_devices 那行。
    `environment` ∈ {"sandbox", "production"}，由 iOS 端
    `aps-environment` entitlements 值决定，Debug = sandbox / Release = production。
    """
    device_id: str
    apns_token: str
    environment: str
    device_name: str | None = None

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def gen_uuid() -> str:
    return str(uuid.uuid4())


# ── Enums ──


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class EventParticipantStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"


class TaskStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"


class ConfirmationType(str, enum.Enum):
    event = "event"
    task = "task"


class ConfirmationStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"
    suggested_alt = "suggested_alt"


class NotificationCategory(str, enum.Enum):
    reminder = "reminder"
    task = "task"
    calendar = "calendar"
    message = "message"
    confirmation = "confirmation"
    system = "system"


# ── Tables ──


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(100))
    # 唯一 handle（@username）—— 与 display_name 分开，加 unique 索引。
    # 历史用户字段为 NULL；EditProfile UI 引导设置后再 PATCH 填充。
    username: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    bio: Mapped[str | None] = mapped_column(String(280), nullable=True)
    auth_type: Mapped[str] = mapped_column(String(20), default="email")
    timezone: Mapped[str] = mapped_column(String(50), default="America/New_York")
    locale: Mapped[str] = mapped_column(String(20), default="en")
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    devices: Mapped[list["UserDevice"]] = relationship(back_populates="user")


class UserFollow(Base):
    """关注关系表 —— follower 关注 followee。组合唯一约束。

    查询模式：
    - 拉某用户的粉丝：WHERE followee_id = ?
    - 拉某用户的关注列表：WHERE follower_id = ?
    - 判断 A 是否关注 B：SELECT 1 WHERE follower_id=A AND followee_id=B
    """
    __tablename__ = "user_follows"
    __table_args__ = (UniqueConstraint("follower_id", "followee_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    follower_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    followee_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserDevice(Base):
    __tablename__ = "user_devices"
    __table_args__ = (UniqueConstraint("user_id", "device_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    device_id: Mapped[str] = mapped_column(String(100))
    device_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    platform: Mapped[str] = mapped_column(String(20), default="ios")
    apns_token: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # `sandbox` (Debug build) or `production` (Release / TestFlight / App Store).
    # 决定 send_push 走 api.sandbox.push.apple.com 还是 api.push.apple.com。
    apns_environment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="devices")


class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    recurrence_rule: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reminder_minutes: Mapped[int] = mapped_column(Integer, default=15)
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    participants: Mapped[list["EventParticipant"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class EventParticipant(Base):
    __tablename__ = "event_participants"
    __table_args__ = (UniqueConstraint("event_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    event_id: Mapped[str] = mapped_column(ForeignKey("calendar_events.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[EventParticipantStatus] = mapped_column(
        Enum(EventParticipantStatus), default=EventParticipantStatus.pending
    )

    event: Mapped["CalendarEvent"] = relationship(back_populates="participants")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    assigned_to: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.pending
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class DeviceRegistry(Base):
    __tablename__ = "device_registry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    homekit_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(100))
    device_type: Mapped[str] = mapped_column(String(50))
    room: Mapped[str | None] = mapped_column(String(100), nullable=True)
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class NotificationRecord(Base):
    __tablename__ = "notification_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    target_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[NotificationCategory] = mapped_column(Enum(NotificationCategory))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    params_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GameStats(Base):
    """每个 AI 游戏的分享 / 点赞计数缓存。

    AI 游戏本身是文件存储（games/{game_id}/meta.json），DB 里没有 Game 记录，
    所以 game_id 这里只是字符串 key，不能加 FK。
    """

    __tablename__ = "game_stats"

    game_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    share_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    like_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class SocialLink(Base):
    """官方社媒 / 网站入口配置 — 由后端运营在数据库中 CRUD，
    iOS 通过公开端点拉取后用 ForEach 驱动 Settings 中的 FOLLOW US 区块。

    设计要点：
    - `platform` 唯一，作为前端识别用（"x" / "discord" / "website" ...）
    - `label_key` 是 iOS Localizable.xcstrings 的 i18n key，缺失时 fall-through
      显示英文 key
    - `icon_name` 是 SF Symbol 名（globe / xmark / camera 等）
    - `url` 可空 — 空表示该行 disabled 灰显
    - `region` 用于按 ROBOT_REGION 过滤，'all' 全展示
    """

    __tablename__ = "social_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    platform: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    label_key: Mapped[str] = mapped_column(String(64), nullable=False)
    icon_name: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    region: Mapped[str] = mapped_column(String(16), default="all", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class GameLike(Base):
    """记录哪个用户点赞了哪个游戏，用于 toggle / 防重复 / liked_by_me 判定。"""

    __tablename__ = "game_likes"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    game_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ── Games marketplace（与 widget marketplace 同构）──

class AIGame(Base):
    """所有游戏（AI 生成的 Godot + 内置原生）的统一元数据表。

    单一来源原则：每个 game_id 只有一行 + filesystem 上一份目录（kind='ai' 时；
    builtin 没有目录，由 iOS 原生 SwiftUI 渲染）。`visibility` 决定它是否出现在
    explore 端点；`kind` 区分内置（无目录、不可 remix）与 AI 生成。

    Remix 不复用此行 — 复制目录 + 新建一行，`original_game_id` 记血缘。
    """

    __tablename__ = "ai_games"

    game_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), default="")
    title_i18n: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_i18n: Mapped[str] = mapped_column(Text)
    icon: Mapped[str] = mapped_column(String(64), default="🎮")
    input_mode: Mapped[str] = mapped_column(
        String(16), default="touch", server_default="touch"
    )
    kind: Mapped[str] = mapped_column(
        String(16), default="ai", server_default="ai"
    )
    visibility: Mapped[str] = mapped_column(
        String(24), default="private", index=True, server_default="private"
    )
    original_game_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    template_from: Mapped[str] = mapped_column(String(64), default="", server_default="")
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    like_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    favorite_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    owner_favorited: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    share_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    remix_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserGameView(Base):
    """每个用户「我的」Tab 看到的游戏列表 — 同时承载自创和收藏。"""

    __tablename__ = "user_game_view"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    game_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    view_type: Mapped[str] = mapped_column(String(16))  # 'created' | 'favorited'
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ExploreGameView(Base):
    """全局「探索区」游戏索引 — 暂时所有用户共享一张表。"""

    __tablename__ = "explore_game_view"

    game_id: Mapped[str] = mapped_column(
        ForeignKey("ai_games.game_id", ondelete="CASCADE"),
        primary_key=True,
    )
    sort_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ── Widget marketplace ──

class UserWidgetData(Base):
    """Per-user 数据快照，AI widget 通过 GET /api/widget-data/{channel} 拉取。

    iOS app 每次刷新一种数据（weather / todos / pomodoro）都把最新值打到这里；
    AI widget 只能 fetch，不传感知。
    """

    __tablename__ = "user_widget_data"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    channel: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AIWidget(Base):
    """所有小组件（AI 生成的 H5 + 内置原生）的统一元数据表。

    单一来源原则：每个 widget_id 只有一行 + filesystem 上一份目录（kind=ai 时）。
    `visibility` 决定它是否出现在 explore 端点；`kind` 区分内置与 AI 生成。

    Remix 不复用此行 — 复制目录 + 新建一行，`original_widget_id` 记血缘。
    """

    __tablename__ = "ai_widgets"

    widget_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    title_i18n: Mapped[str] = mapped_column(Text)
    description_i18n: Mapped[str] = mapped_column(Text)
    supported_sizes: Mapped[str] = mapped_column(String(128))  # JSON array
    base_template_id: Mapped[str] = mapped_column(String(64), default="")
    visibility: Mapped[str] = mapped_column(
        String(24), default="private", index=True, server_default="private"
    )
    kind: Mapped[str] = mapped_column(
        String(16), default="ai", server_default="ai"
    )
    original_widget_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    like_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    favorite_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    remix_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WidgetLike(Base):
    """记录哪个用户点赞了哪个小组件。`ai_widgets.like_count` 是这张表的 cache。"""

    __tablename__ = "widget_likes"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    widget_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WidgetFavorite(Base):
    """与 `user_widget_view` 解耦的纯收藏关系（拆开是为了让作者也能收藏自己的组件）。"""

    __tablename__ = "widget_favorites"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    widget_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserWidgetView(Base):
    """每个用户「我的」Tab 看到的小组件列表 — 只放 'created' (favorite 走独立表)。"""

    __tablename__ = "user_widget_view"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    widget_id: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)
    view_type: Mapped[str] = mapped_column(String(16))  # 'created' | 'favorited'
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ExploreWidgetView(Base):
    """全局「探索区」widget 索引 — 暂时所有用户共享一张表。"""

    __tablename__ = "explore_widget_view"

    widget_id: Mapped[str] = mapped_column(
        ForeignKey("ai_widgets.widget_id", ondelete="CASCADE"),
        primary_key=True,
    )
    sort_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PendingConfirmation(Base):
    __tablename__ = "pending_confirmations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=gen_uuid)
    type: Mapped[ConfirmationType] = mapped_column(Enum(ConfirmationType))
    target_id: Mapped[str] = mapped_column(String(36))
    from_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    to_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[ConfirmationStatus] = mapped_column(
        Enum(ConfirmationStatus), default=ConfirmationStatus.pending
    )
    alt_suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

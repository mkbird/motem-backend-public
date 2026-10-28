"""add widget marketplace tables and columns

Revision ID: g7h8i9j0k1l2
Revises: f6a7b8c9d0e1
Create Date: 2026-04-27 00:00:00.000000

把 ai_widgets 升级为完整的小组件市场后端：
- ai_widgets 加 visibility / kind / original_widget_id / 三个 count 列；
  owner_id 改 nullable（让内置 kind='builtin' 行 owner_id=NULL）。
- 新增 widget_likes（点赞关系）、user_widget_view（per-user "我的" 视图）、
  explore_widget_view（全局 "探索区" 视图）。
- 种子 5 个内置原生小组件（time / weather / todo / focus / photo），
  以 kind='builtin'、owner_id=NULL、visibility='public_view_only' 入库，
  并同时插入 explore_widget_view（让所有用户在探索区看到）。
- 现存 AI 小组件（kind='ai' 默认）回填 visibility='private' 保守，
  同时把 (owner_id, widget_id, 'created') 写到 user_widget_view，避免老数据
  无法在 Mine Tab 显示。
"""
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g7h8i9j0k1l2"
down_revision: Union[str, None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# i18n title/description 与 iOS motem/AddWidgetSheet.swift 中
# `private extension ProductCard { displayName / summary }` 的 LocalizedStringKey 对齐。
BUILTIN_SEEDS = [
    {
        "widget_id": "builtin_time",
        "title_i18n": {"en": "Time", "zh-Hans": "时间"},
        "description_i18n": {
            "en": "Current time and date",
            "zh-Hans": "当前时间和日期",
        },
    },
    {
        "widget_id": "builtin_weather",
        "title_i18n": {"en": "Weather", "zh-Hans": "天气"},
        "description_i18n": {
            "en": "Temperature and forecast",
            "zh-Hans": "温度和天气预报",
        },
    },
    {
        "widget_id": "builtin_todo",
        "title_i18n": {"en": "Todo", "zh-Hans": "待办"},
        "description_i18n": {
            "en": "Task list and progress",
            "zh-Hans": "任务列表和进度",
        },
    },
    {
        "widget_id": "builtin_focus",
        "title_i18n": {"en": "Focus", "zh-Hans": "专注"},
        "description_i18n": {
            "en": "Pomodoro focus timer",
            "zh-Hans": "番茄专注计时器",
        },
    },
    {
        "widget_id": "builtin_photo",
        "title_i18n": {"en": "Photos", "zh-Hans": "照片"},
        "description_i18n": {
            "en": "Personal photo gallery",
            "zh-Hans": "个人照片相册",
        },
    },
]


def upgrade() -> None:
    # ── 1. 扩展 ai_widgets 列 ──
    with op.batch_alter_table("ai_widgets") as batch:
        batch.alter_column(
            "owner_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
        batch.add_column(
            sa.Column(
                "visibility",
                sa.String(length=24),
                nullable=False,
                server_default="private",
            )
        )
        batch.add_column(
            sa.Column(
                "kind",
                sa.String(length=16),
                nullable=False,
                server_default="ai",
            )
        )
        batch.add_column(
            sa.Column("original_widget_id", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "like_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "favorite_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "remix_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )

    op.create_index("ix_ai_widgets_visibility", "ai_widgets", ["visibility"])
    op.create_index(
        "ix_ai_widgets_original_widget_id", "ai_widgets", ["original_widget_id"]
    )

    # ── 2. 三张视图 / 关系表 ──
    op.create_table(
        "widget_likes",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("widget_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "widget_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="fk_widget_likes_user",
        ),
    )
    op.create_index("ix_widget_likes_widget_id", "widget_likes", ["widget_id"])

    op.create_table(
        "user_widget_view",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("widget_id", sa.String(length=64), nullable=False),
        sa.Column("view_type", sa.String(length=16), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "widget_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="fk_user_widget_view_user",
        ),
    )
    op.create_index(
        "ix_user_widget_view_widget_id", "user_widget_view", ["widget_id"]
    )

    op.create_table(
        "explore_widget_view",
        sa.Column("widget_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "sort_score", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["widget_id"], ["ai_widgets.widget_id"], ondelete="CASCADE",
            name="fk_explore_widget_view_widget",
        ),
    )

    # ── 3. 数据迁移 ──
    bind = op.get_bind()
    now = datetime.utcnow()
    now_str = now.replace(microsecond=0).isoformat(sep=" ")

    # 3a. 现存 AI 小组件 → user_widget_view (owner, id, 'created')
    bind.execute(
        sa.text(
            """
            INSERT INTO user_widget_view (user_id, widget_id, view_type, added_at)
            SELECT owner_id, widget_id, 'created', :now
              FROM ai_widgets
             WHERE owner_id IS NOT NULL
            """
        ),
        {"now": now_str},
    )

    # 3b. 种子 5 个内置 → ai_widgets + explore_widget_view
    import json as _json

    for seed in BUILTIN_SEEDS:
        bind.execute(
            sa.text(
                """
                INSERT INTO ai_widgets (
                    widget_id, owner_id,
                    title_i18n, description_i18n,
                    supported_sizes, base_template_id,
                    visibility, kind,
                    original_widget_id,
                    like_count, favorite_count, remix_count,
                    created_at, updated_at
                ) VALUES (
                    :widget_id, NULL,
                    :title_i18n, :description_i18n,
                    :supported_sizes, '',
                    'public_view_only', 'builtin',
                    NULL,
                    0, 0, 0,
                    :now, :now
                )
                """
            ),
            {
                "widget_id": seed["widget_id"],
                "title_i18n": _json.dumps(seed["title_i18n"], ensure_ascii=False),
                "description_i18n": _json.dumps(
                    seed["description_i18n"], ensure_ascii=False
                ),
                "supported_sizes": _json.dumps(
                    ["small", "medium", "large", "xlarge"]
                ),
                "now": now_str,
            },
        )
        bind.execute(
            sa.text(
                """
                INSERT INTO explore_widget_view (widget_id, sort_score, added_at)
                VALUES (:widget_id, 0, :now)
                """
            ),
            {"widget_id": seed["widget_id"], "now": now_str},
        )


def downgrade() -> None:
    op.drop_table("explore_widget_view")
    op.drop_index("ix_user_widget_view_widget_id", table_name="user_widget_view")
    op.drop_table("user_widget_view")
    op.drop_index("ix_widget_likes_widget_id", table_name="widget_likes")
    op.drop_table("widget_likes")

    # 删除内置种子（避免 downgrade 残留 owner_id=NULL 的 builtin 行）
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM ai_widgets WHERE kind = 'builtin'"))

    op.drop_index("ix_ai_widgets_original_widget_id", table_name="ai_widgets")
    op.drop_index("ix_ai_widgets_visibility", table_name="ai_widgets")
    with op.batch_alter_table("ai_widgets") as batch:
        batch.drop_column("remix_count")
        batch.drop_column("favorite_count")
        batch.drop_column("like_count")
        batch.drop_column("original_widget_id")
        batch.drop_column("kind")
        batch.drop_column("visibility")
        batch.alter_column(
            "owner_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )

"""split widget favorites from user_widget_view

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-04-27 11:00:00.000000

把 favorited 关系从 `user_widget_view` 抽成独立的 `widget_favorites` 表。

之前 `user_widget_view` 的 PK 是 (user_id, widget_id)，view_type 列区分
created / favorited。这让作者无法 favorite 自己的组件（'created' 行已占
位置），用户报告希望支持收藏自己的作品。

新模型：
- `user_widget_view` 只保留 view_type='created' 行（仍是用户创建过的组件
  索引）
- `widget_favorites` 新表，纯收藏关系，PK (user_id, widget_id)
- Mine 端点 = owner UNION favorites（去重）
"""
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h8i9j0k1l2m3"
down_revision: Union[str, None] = "g7h8i9j0k1l2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. 新建 widget_favorites
    op.create_table(
        "widget_favorites",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("widget_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "widget_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="fk_widget_favorites_user",
        ),
    )
    op.create_index(
        "ix_widget_favorites_widget_id", "widget_favorites", ["widget_id"]
    )

    # 2. 把 user_widget_view 中所有 view_type='favorited' 的行迁过去
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO widget_favorites (user_id, widget_id, created_at)
            SELECT user_id, widget_id, added_at
              FROM user_widget_view
             WHERE view_type = 'favorited'
            """
        )
    )

    # 3. 从 user_widget_view 删除已迁出的 favorited 行
    bind.execute(
        sa.text("DELETE FROM user_widget_view WHERE view_type = 'favorited'")
    )


def downgrade() -> None:
    bind = op.get_bind()
    now_str = datetime.utcnow().replace(microsecond=0).isoformat(sep=" ")
    # 把 widget_favorites 行写回 user_widget_view (view_type='favorited')
    bind.execute(
        sa.text(
            """
            INSERT INTO user_widget_view (user_id, widget_id, view_type, added_at)
            SELECT user_id, widget_id, 'favorited', created_at
              FROM widget_favorites
             WHERE NOT EXISTS (
                SELECT 1 FROM user_widget_view v
                 WHERE v.user_id = widget_favorites.user_id
                   AND v.widget_id = widget_favorites.widget_id
             )
            """
        )
    )
    op.drop_index("ix_widget_favorites_widget_id", table_name="widget_favorites")
    op.drop_table("widget_favorites")

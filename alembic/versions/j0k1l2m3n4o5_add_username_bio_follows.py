"""add user.username + user.bio + user_follows table

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-05-07 21:00:00.000000

社交基础设施 Phase B：
- users.username (varchar(20), unique, nullable) —— 与 display_name 分开的 @handle
- users.bio (varchar(280), nullable)
- user_follows (follower_id, followee_id) 关注关系表，组合唯一

历史用户 username = NULL；客户端进 EditProfile 引导设置。这样不需要在迁移里
为每行造一个 placeholder（避免 collision 风险）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "j0k1l2m3n4o5"
down_revision: Union[str, None] = "i9j0k1l2m3n4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # users.username — unique nullable
    op.add_column(
        "users",
        sa.Column("username", sa.String(20), nullable=True),
    )
    op.create_unique_constraint("uq_users_username", "users", ["username"])

    # users.bio
    op.add_column(
        "users",
        sa.Column("bio", sa.String(280), nullable=True),
    )

    # user_follows 表
    op.create_table(
        "user_follows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("follower_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("followee_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("follower_id", "followee_id", name="uq_user_follows_pair"),
    )
    op.create_index("ix_user_follows_follower_id", "user_follows", ["follower_id"])
    op.create_index("ix_user_follows_followee_id", "user_follows", ["followee_id"])


def downgrade() -> None:
    op.drop_index("ix_user_follows_followee_id", table_name="user_follows")
    op.drop_index("ix_user_follows_follower_id", table_name="user_follows")
    op.drop_table("user_follows")
    op.drop_column("users", "bio")
    op.drop_constraint("uq_users_username", "users", type_="unique")
    op.drop_column("users", "username")

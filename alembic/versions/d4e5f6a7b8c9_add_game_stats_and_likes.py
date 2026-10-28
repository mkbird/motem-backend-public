"""add game stats and likes tables

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "game_stats",
        sa.Column("game_id", sa.String(length=64), primary_key=True),
        sa.Column("share_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("like_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "game_likes",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("game_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "game_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE",
            name="fk_game_likes_user",
        ),
    )
    op.create_index("ix_game_likes_game_id", "game_likes", ["game_id"])


def downgrade() -> None:
    op.drop_index("ix_game_likes_game_id", table_name="game_likes")
    op.drop_table("game_likes")
    op.drop_table("game_stats")

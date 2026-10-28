"""add ai_games.owner_favorited column

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-04-28 00:00:00.000000

让作者本人也能收藏自己的游戏。owner 永远只有一个，per-game 一个 boolean
就足够记录"作者是否 favorited 自己"。toggle_favorite endpoint 对 owner
走这一列，对非 owner 仍走 user_game_view view_type='favorited'。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "i9j0k1l2m3n4"
down_revision: Union[str, None] = "h8i9j0k1l2m3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_games",
        sa.Column(
            "owner_favorited",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_games", "owner_favorited")

"""disable instagram and tiktok social links

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-04-25 04:00:00.000000

产品决定 MVP 阶段只展示官网 / X / Discord 三条。Instagram 和 TikTok
暂用 enabled=false 软隐藏（不删行，未来恢复时只需一条 UPDATE）。
"""
from typing import Sequence, Union

from alembic import op


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DISABLED_PLATFORMS = ("instagram", "tiktok")


def upgrade() -> None:
    for platform in _DISABLED_PLATFORMS:
        op.execute(
            f"UPDATE social_links SET enabled=0 WHERE platform='{platform}'"
        )


def downgrade() -> None:
    for platform in _DISABLED_PLATFORMS:
        op.execute(
            f"UPDATE social_links SET enabled=1 WHERE platform='{platform}'"
        )

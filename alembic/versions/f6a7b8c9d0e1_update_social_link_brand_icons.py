"""update social_links icon_name to logo_* for brand glyphs

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-04-25 03:30:00.000000

iOS 端用自绘 SwiftUI Path 渲染 4 个品牌 monochrome glyph（X / Discord /
Instagram / TikTok），icon_name 走 `logo_*` 前缀；后续若想换更精确的资产，
仅 iOS SocialIconView 改一处。
"""
from typing import Sequence, Union

from alembic import op


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPDATES = [
    ("x",         "logo_x"),
    ("discord",   "logo_discord"),
    ("instagram", "logo_instagram"),
    ("tiktok",    "logo_tiktok"),
]

_REVERTS = [
    ("x",         "xmark"),
    ("discord",   "bubble.left.and.bubble.right"),
    ("instagram", "camera"),
    ("tiktok",    "music.note"),
]


def upgrade() -> None:
    for platform, icon in _UPDATES:
        op.execute(
            f"UPDATE social_links SET icon_name='{icon}' WHERE platform='{platform}'"
        )


def downgrade() -> None:
    for platform, icon in _REVERTS:
        op.execute(
            f"UPDATE social_links SET icon_name='{icon}' WHERE platform='{platform}'"
        )

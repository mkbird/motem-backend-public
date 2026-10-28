"""add social_links table + seed initial 5 rows

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-25 03:00:00.000000

"""
import uuid
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "social_links",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("platform", sa.String(length=32), nullable=False, unique=True),
        sa.Column("label_key", sa.String(length=64), nullable=False),
        sa.Column("icon_name", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("region", sa.String(length=16), nullable=False, server_default="all"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_social_links_enabled_sort",
        "social_links",
        ["enabled", "sort_order"],
    )

    # 种 5 条初始记录 — URL 全 NULL，由运营后续 UPDATE 填
    now = datetime.utcnow()
    seeds = [
        {
            "platform": "website",
            "label_key": "Official Website",
            "icon_name": "globe",
            "sort_order": 10,
        },
        {
            "platform": "x",
            "label_key": "Follow Us on X",
            "icon_name": "logo_x",
            "sort_order": 20,
        },
        {
            "platform": "discord",
            "label_key": "Follow Us on Discord",
            "icon_name": "logo_discord",
            "sort_order": 30,
        },
        {
            "platform": "instagram",
            "label_key": "Follow Us on Instagram",
            "icon_name": "logo_instagram",
            "sort_order": 40,
        },
        {
            "platform": "tiktok",
            "label_key": "Follow Us on TikTok",
            "icon_name": "logo_tiktok",
            "sort_order": 50,
        },
    ]

    social_links = sa.table(
        "social_links",
        sa.column("id", sa.String),
        sa.column("platform", sa.String),
        sa.column("label_key", sa.String),
        sa.column("icon_name", sa.String),
        sa.column("url", sa.String),
        sa.column("sort_order", sa.Integer),
        sa.column("enabled", sa.Boolean),
        sa.column("region", sa.String),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    op.bulk_insert(
        social_links,
        [
            {
                "id": str(uuid.uuid4()),
                "platform": s["platform"],
                "label_key": s["label_key"],
                "icon_name": s["icon_name"],
                "url": None,
                "sort_order": s["sort_order"],
                "enabled": True,
                "region": "all",
                "created_at": now,
                "updated_at": now,
            }
            for s in seeds
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_social_links_enabled_sort", table_name="social_links")
    op.drop_table("social_links")

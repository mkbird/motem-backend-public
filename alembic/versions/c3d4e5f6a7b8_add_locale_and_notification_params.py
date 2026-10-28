"""add locale and notification params

Revision ID: c3d4e5f6a7b8
Revises: 2057655db7ee
Create Date: 2026-04-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "2057655db7ee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("locale", sa.String(length=20), nullable=False, server_default="en"),
    )
    op.add_column(
        "notification_records",
        sa.Column("params_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_records", "params_json")
    op.drop_column("users", "locale")

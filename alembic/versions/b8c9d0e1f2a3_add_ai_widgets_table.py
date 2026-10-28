"""add ai_widgets table

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-04-26 00:00:00.000000

H5 widget metadata + ownership. File contents live on disk under
`widgets/{widget_id}/`; this table is keyed by widget_id and records:

- owner_id (FK users.id ON DELETE CASCADE)
- title_i18n / description_i18n (JSON-as-text)
- supported_sizes (JSON array)
- base_template_id (seed or other widget the user cloned)
- created_at / updated_at

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_widgets",
        sa.Column("widget_id", sa.String(length=64), primary_key=True),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("title_i18n", sa.Text(), nullable=False),
        sa.Column("description_i18n", sa.Text(), nullable=False),
        sa.Column("supported_sizes", sa.String(length=128), nullable=False),
        sa.Column("base_template_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], ondelete="CASCADE",
            name="fk_ai_widgets_owner",
        ),
    )
    op.create_index("ix_ai_widgets_owner_id", "ai_widgets", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_widgets_owner_id", table_name="ai_widgets")
    op.drop_table("ai_widgets")

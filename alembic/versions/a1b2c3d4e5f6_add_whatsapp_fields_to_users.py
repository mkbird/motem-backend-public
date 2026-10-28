"""add whatsapp_id and auth_type to users

Revision ID: a1b2c3d4e5f6
Revises: 708336f0ba4d
Create Date: 2026-03-22 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "708336f0ba4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("whatsapp_id", sa.String(50), nullable=True))
    op.add_column(
        "users", sa.Column("auth_type", sa.String(20), nullable=False, server_default="email")
    )
    op.create_index("ix_users_whatsapp_id", "users", ["whatsapp_id"], unique=True)

    # Make email and password_hash nullable for whatsapp_only users
    op.alter_column("users", "email", existing_type=sa.String(255), nullable=True)
    op.alter_column("users", "password_hash", existing_type=sa.String(255), nullable=True)


def downgrade() -> None:
    op.alter_column("users", "password_hash", existing_type=sa.String(255), nullable=False)
    op.alter_column("users", "email", existing_type=sa.String(255), nullable=False)
    op.drop_index("ix_users_whatsapp_id", table_name="users")
    op.drop_column("users", "auth_type")
    op.drop_column("users", "whatsapp_id")

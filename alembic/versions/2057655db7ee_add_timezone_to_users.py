"""add_timezone_to_users

Revision ID: 2057655db7ee
Revises: b2c3d4e5f6a7
Create Date: 2026-04-11 13:05:32.345314

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '2057655db7ee'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('timezone', sa.String(length=50), nullable=False, server_default='America/New_York'))


def downgrade() -> None:
    op.drop_column('users', 'timezone')

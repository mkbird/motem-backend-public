"""add apns_environment to user_devices

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-05-18

iOS Debug build 写 `sandbox`，Release / TestFlight / App Store 写 `production`，
推送时按这一列路由到 api.sandbox.push.apple.com / api.push.apple.com。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "k1l2m3n4o5p6"
down_revision: Union[str, None] = "j0k1l2m3n4o5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_devices",
        sa.Column("apns_environment", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_devices", "apns_environment")

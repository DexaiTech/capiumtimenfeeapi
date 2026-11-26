"""opening balance flag on time entries

Revision ID: 045
Revises: 044

Adds ``time_entries.is_opening_balance``: set on rows created by the opening-balance import
(``POST /organizations/{id}/imports/opening-balance-time``) so they can be shown separately
from newly recorded time, while still flowing through the normal WIP and billing allocation
unchanged (``WipAllocation`` does not distinguish imported time from new time, so both can be
billed together without double counting, per the client's acceptance example).

The ``opening_balance.import`` and ``data.import_export`` permissions already exist and are
already granted to the relevant roles (migration 019); nothing to add here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "045"
down_revision: str | None = "044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "time_entries",
        sa.Column(
            "is_opening_balance",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("time_entries", "is_opening_balance")

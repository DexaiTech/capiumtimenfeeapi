"""client segments switch

Revision ID: 075
Revises: 074

Ticket APT70: a practice can stop using client segments. ``organizations.client_segments_enabled``
is on for every practice, so nothing changes until one switches it off. Off, the UI hides the
segments everywhere and the client export leaves the column out; the segments, the staff linked to
them and the clients' values are kept and come back when it is switched on again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "075"
down_revision: str | None = "074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "client_segments_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )


def downgrade() -> None:
    op.drop_column("organizations", "client_segments_enabled")

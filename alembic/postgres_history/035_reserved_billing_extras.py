"""reserved for the billing_extras engineer: filled in by that engineer, empty until then

Revision ID: 035
Revises: 034
"""

from collections.abc import Sequence

revision: str = "035"
down_revision: str | None = "034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

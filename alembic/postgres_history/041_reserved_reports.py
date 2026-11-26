"""reserved for the reports engineer: filled in by that engineer, empty until then

Revision ID: 041
Revises: 040
"""

from collections.abc import Sequence

revision: str = "041"
down_revision: str | None = "040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

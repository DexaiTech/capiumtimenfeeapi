"""billing changes

Reserved for the billing agent. Empty until that module needs a schema change.

Revision ID: 009
Revises: 008
"""

from collections.abc import Sequence

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

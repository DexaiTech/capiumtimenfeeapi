"""clients settings and rates changes

Reserved for the clients, settings and rates agent. Empty until that module needs a schema change.

Revision ID: 007
Revises: 006
"""

from collections.abc import Sequence

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

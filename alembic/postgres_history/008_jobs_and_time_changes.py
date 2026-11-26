"""jobs and time changes

Reserved for the jobs, time and timesheets agent. Empty until that module needs a schema change.

Revision ID: 008
Revises: 007
"""

from collections.abc import Sequence

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

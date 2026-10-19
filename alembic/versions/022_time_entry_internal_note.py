"""time entries: internal note

An internal note (up to 2000 characters) next to the description: it is never printed on an
invoice and only the person, people who read all time and people who manage all time see it.

Revision ID: 022
Revises: 021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "022"
down_revision: str | None = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("time_entries", sa.Column("internal_note", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_time_entries_internal_note_length"),
        "time_entries",
        "internal_note IS NULL OR length(internal_note) <= 2000",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_time_entries_internal_note_length"), "time_entries", type_="check")
    op.drop_column("time_entries", "internal_note")

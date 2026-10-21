"""rich text in tickets and comments

Revision ID: 048
Revises: 047

Adds ``tickets.description_html`` and ``ticket_comments.body_html``: the sanitised HTML the rich
text editor produces. The plain ``description`` and ``body`` stay, filled with the text of that
HTML, so search, excerpts, e-mails and the audit log work as before and rows written before this
migration (plain text only, ``*_html`` null) are shown as they were.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "048"
down_revision: str | None = "047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("description_html", sa.Text(), nullable=True))
    op.create_check_constraint(
        "description_html_length",
        "tickets",
        "description_html IS NULL OR length(description_html) <= 40000",
    )
    op.add_column("ticket_comments", sa.Column("body_html", sa.Text(), nullable=True))
    op.create_check_constraint(
        "body_html_length",
        "ticket_comments",
        "body_html IS NULL OR length(body_html) <= 20000",
    )


def downgrade() -> None:
    op.drop_constraint("body_html_length", "ticket_comments", type_="check")
    op.drop_column("ticket_comments", "body_html")
    op.drop_constraint("description_html_length", "tickets", type_="check")
    op.drop_column("tickets", "description_html")

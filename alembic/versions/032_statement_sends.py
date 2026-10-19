"""statement sends: the log of statements emailed to clients

One row per statement queued for a client (who asked, the period, the recipients, the closing
balance at that moment and whether the email went).

Revision ID: 032
Revises: 031
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "032"
down_revision: str | None = "031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "statement_sends",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("date_from", sa.Date(), nullable=True),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("recipients", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("closing_balance", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("sent_by", sa.UUID(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'sent', 'failed')", name=op.f("ck_statement_sends_status")
        ),
        sa.CheckConstraint(
            "date_from IS NULL OR date_from <= date_to", name=op.f("ck_statement_sends_period")
        ),
        sa.CheckConstraint(
            "cardinality(recipients) >= 1", name=op.f("ck_statement_sends_has_recipients")
        ),
        sa.CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)", name=op.f("ck_statement_sends_sent_at")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_statement_sends_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_statement_sends_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sent_by"], ["users.id"], name=op.f("fk_statement_sends_sent_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statement_sends")),
        sa.UniqueConstraint(
            "organization_id", "id", name=op.f("uq_statement_sends_organization_id_id")
        ),
    )
    add_updated_at_triggers("statement_sends")
    op.create_index(
        "ix_statement_sends_organization_id_client_id_created_at",
        "statement_sends",
        ["organization_id", "client_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("statement_sends")

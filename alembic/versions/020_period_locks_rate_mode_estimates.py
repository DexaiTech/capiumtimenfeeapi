"""period locks, estimate numbering and the rate mode

Adds the practice's period lock (``period_locks``: one row per practice, and
``period_lock_events``: the append-only history, which a trigger protects from every UPDATE and
every direct DELETE), the ``estimate`` number sequence (``EST00001``) and
``organizations.rate_mode`` (``staff_first`` or ``grade_only``). Existing practices get an empty
lock and the estimate sequence.

Revision ID: 020
Revises: 019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "020"
down_revision: str | None = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS_BEFORE = "'client', 'job', 'invoice', 'credit_note', 'staff', 'ticket'"
KINDS_AFTER = f"{KINDS_BEFORE}, 'estimate'"


def _organization_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("rate_mode", sa.Text(), server_default="staff_first", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_organizations_rate_mode"),
        "organizations",
        "rate_mode IN ('staff_first', 'grade_only')",
    )

    # Estimates are numbered EST00001, EST00002, ... and, like invoices, only forward.
    op.drop_constraint(op.f("ck_document_sequences_kind"), "document_sequences", type_="check")
    op.create_check_constraint(
        op.f("ck_document_sequences_kind"), "document_sequences", f"kind IN ({KINDS_AFTER})"
    )
    op.execute(
        """
        INSERT INTO document_sequences (organization_id, kind, prefix, next_number, padding)
        SELECT id, 'estimate', 'EST', 1, 5 FROM organizations
        ON CONFLICT (organization_id, kind) DO NOTHING
        """
    )

    op.create_table(
        "period_locks",
        sa.Column("locked_through", sa.Date(), nullable=True),
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
        _organization_fk("period_locks"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_period_locks")),
        sa.UniqueConstraint("organization_id", name="uq_period_locks_organization_id"),
        sa.UniqueConstraint("organization_id", "id", name="uq_period_locks_organization_id_id"),
    )
    add_updated_at_triggers("period_locks")
    op.execute(
        "INSERT INTO period_locks (id, organization_id) SELECT gen_random_uuid(), id "
        "FROM organizations"
    )

    op.create_table(
        "period_lock_events",
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("previous_locked_through", sa.Date(), nullable=True),
        sa.Column("locked_through", sa.Date(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_user_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('locked', 'reopened')", name=op.f("ck_period_lock_events_event_type")
        ),
        sa.CheckConstraint(
            "reason IS NULL OR length(reason) BETWEEN 1 AND 500",
            name=op.f("ck_period_lock_events_reason"),
        ),
        sa.CheckConstraint(
            "event_type <> 'reopened' OR reason IS NOT NULL",
            name=op.f("ck_period_lock_events_reopen_reason"),
        ),
        sa.CheckConstraint(
            "event_type <> 'locked' OR (locked_through IS NOT NULL AND "
            "(previous_locked_through IS NULL OR locked_through > previous_locked_through))",
            name=op.f("ck_period_lock_events_locked_moves_forward"),
        ),
        sa.CheckConstraint(
            "event_type <> 'reopened' OR (previous_locked_through IS NOT NULL AND "
            "(locked_through IS NULL OR locked_through < previous_locked_through))",
            name=op.f("ck_period_lock_events_reopened_moves_back"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_period_lock_events_organization_id_actor_members",
        ),
        _organization_fk("period_lock_events"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_period_lock_events")),
    )
    op.create_index(
        "ix_period_lock_events_organization_id_created_at",
        "period_lock_events",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    # The history is append-only. A practice's rows go with the practice: the foreign key's
    # cascade deletes them one trigger level deeper than a DELETE that somebody typed.
    op.execute(
        """
        CREATE FUNCTION reject_period_lock_event_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'period_lock_events is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_period_lock_events_append_only BEFORE UPDATE OR DELETE "
        "ON period_lock_events FOR EACH ROW EXECUTE FUNCTION reject_period_lock_event_change()"
    )


def downgrade() -> None:
    op.drop_table("period_lock_events")
    op.execute("DROP FUNCTION reject_period_lock_event_change()")
    op.drop_table("period_locks")

    op.execute("DELETE FROM document_sequences WHERE kind = 'estimate'")
    op.drop_constraint(op.f("ck_document_sequences_kind"), "document_sequences", type_="check")
    op.create_check_constraint(
        op.f("ck_document_sequences_kind"), "document_sequences", f"kind IN ({KINDS_BEFORE})"
    )

    op.drop_constraint(op.f("ck_organizations_rate_mode"), "organizations", type_="check")
    op.drop_column("organizations", "rate_mode")

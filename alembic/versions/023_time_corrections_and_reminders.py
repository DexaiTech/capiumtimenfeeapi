"""time: correction history of approved time and timesheet reminders

``time_entry_history`` keeps the before and after of every correction of approved, unbilled
time, with the reason. It is append-only: a trigger refuses every UPDATE and every DELETE that
somebody typed. The practice's own deletion still removes its rows (the foreign key's cascade
runs one trigger level deeper) and so does TRUNCATE (used by the maintenance reset and the tests,
it is not row-level). Like ``rate_changes`` it keeps the ids it names without foreign keys, so it
outlives an entry that is deleted later and never blocks deleting or merging staff.

``timesheet_reminders`` records which person was reminded about which week (``timesheet``: a
missing timesheet; ``approval``: timesheets waiting for approval) so that a reminder is sent
once per person and week and the reminder screens can show when it was last sent.

Revision ID: 023
Revises: 022
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "023"
down_revision: str | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "time_entry_history",
        sa.Column("time_entry_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("corrected_by", sa.UUID(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "length(btrim(reason)) BETWEEN 1 AND 2000",
            name=op.f("ck_time_entry_history_reason_length"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(before) = 'object' AND jsonb_typeof(after) = 'object'",
            name=op.f("ck_time_entry_history_snapshots_are_objects"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_time_entry_history_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_time_entry_history")),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_time_entry_history_organization_id_id"
        ),
    )
    op.create_index(
        "ix_time_entry_history_organization_id_time_entry_id_created_at",
        "time_entry_history",
        ["organization_id", "time_entry_id", "created_at"],
        unique=False,
    )
    # The history is append-only. A practice's rows go with the practice: the foreign key's
    # cascade deletes them one trigger level deeper than a DELETE that somebody typed.
    op.execute(
        """
        CREATE FUNCTION prevent_time_entry_history_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'time_entry_history is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_time_entry_history_append_only BEFORE UPDATE OR DELETE "
        "ON time_entry_history FOR EACH ROW EXECUTE FUNCTION prevent_time_entry_history_change()"
    )

    op.create_table(
        "timesheet_reminders",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "reminded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('timesheet', 'approval')", name=op.f("ck_timesheet_reminders_kind")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_timesheet_reminders_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_timesheet_reminders")),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "week_start",
            "kind",
            name="uq_timesheet_reminders_organization_id_user_id_week_start_kind",
        ),
    )
    op.create_index(
        "ix_timesheet_reminders_organization_id_week_start_kind",
        "timesheet_reminders",
        ["organization_id", "week_start", "kind"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_timesheet_reminders_organization_id_week_start_kind", "timesheet_reminders")
    op.drop_table("timesheet_reminders")
    op.execute("DROP TRIGGER trg_time_entry_history_append_only ON time_entry_history")
    op.drop_index(
        "ix_time_entry_history_organization_id_time_entry_id_created_at", "time_entry_history"
    )
    op.drop_table("time_entry_history")
    op.execute("DROP FUNCTION prevent_time_entry_history_change()")

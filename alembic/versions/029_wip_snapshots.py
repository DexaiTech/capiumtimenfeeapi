"""historic WIP: the frozen WIP of a closed day

``wip_snapshots`` (one per practice and day) and ``wip_snapshot_items`` hold the WIP of a day as it
was, written the first time that day is reported. Both are append-only (the trigger function of
migration 028 refuses every UPDATE and every direct DELETE), so a later invoice, write-off or rate
change can never alter a figure that was reported. The items keep the client, job, people and names
as they were; they have no foreign keys to them, like the other history tables.

Revision ID: 029
Revises: 028
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "029"
down_revision: str | None = "028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _organization_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        "wip_snapshots",
        sa.Column("as_at", sa.Date(), nullable=False),
        sa.Column("source", sa.Text(), server_default="request", nullable=False),
        sa.Column("fixed_fee_wip_mode", sa.Text(), nullable=False),
        sa.Column("require_timesheet_approval", sa.Boolean(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("total", sa.BigInteger(), nullable=False),
        sa.Column("taken_by", sa.UUID(), nullable=True),
        sa.Column(
            "taken_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "source IN ('request', 'scheduled')", name=op.f("ck_wip_snapshots_source")
        ),
        sa.CheckConstraint(
            "fixed_fee_wip_mode IN ('on_completion', 'by_budget_progress')",
            name=op.f("ck_wip_snapshots_fixed_fee_wip_mode"),
        ),
        sa.CheckConstraint("item_count >= 0", name=op.f("ck_wip_snapshots_item_count")),
        _organization_fk("wip_snapshots"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wip_snapshots")),
        sa.UniqueConstraint("organization_id", "id", name="uq_wip_snapshots_organization_id_id"),
        sa.UniqueConstraint(
            "organization_id", "as_at", name="uq_wip_snapshots_organization_id_as_at"
        ),
    )
    op.create_table(
        "wip_snapshot_items",
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("item_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("client_code", sa.Text(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("job_number", sa.Text(), nullable=True),
        sa.Column("job_name", sa.Text(), nullable=True),
        sa.Column("billing_basis", sa.Text(), nullable=True),
        sa.Column("partner_user_id", sa.UUID(), nullable=True),
        sa.Column("manager_user_id", sa.UUID(), nullable=True),
        sa.Column("service_id", sa.UUID(), nullable=True),
        sa.Column("person_user_id", sa.UUID(), nullable=True),
        sa.Column("item_date", sa.Date(), nullable=False),
        sa.Column("minutes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("charge_value", sa.BigInteger(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('time', 'fixed_fee', 'expense', 'disbursement', 'adjustment')",
            name=op.f("ck_wip_snapshot_items_kind"),
        ),
        sa.CheckConstraint(
            "component IN ('time', 'fixed_fee', 'expense')",
            name=op.f("ck_wip_snapshot_items_component"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "snapshot_id"],
            ["wip_snapshots.organization_id", "wip_snapshots.id"],
            name="fk_wip_snapshot_items_snapshot_id_wip_snapshots",
            ondelete="CASCADE",
        ),
        _organization_fk("wip_snapshot_items"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wip_snapshot_items")),
    )
    op.create_index(
        "ix_wip_snapshot_items_snapshot_id_client_id_job_id",
        "wip_snapshot_items",
        ["snapshot_id", "client_id", "job_id"],
    )
    op.create_index(
        "ix_wip_snapshot_items_snapshot_id_item_date",
        "wip_snapshot_items",
        ["snapshot_id", "item_date", "id"],
    )
    for table in ("wip_snapshots", "wip_snapshot_items"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE "
            f"ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_wip_history_change()"
        )


def downgrade() -> None:
    op.drop_table("wip_snapshot_items")
    op.drop_table("wip_snapshots")

"""WIP ledger, adjustments, transfers and credit-note WIP

* ``wip_movements``: the immutable WIP ledger. A trigger refuses every UPDATE and every direct
  DELETE (a practice's rows still go with the practice: the foreign key's cascade runs one trigger
  level deeper); a correction is a new ``reversal`` row. Client and job are plain ids: it is history.
  The write-offs that already exist are copied in as memo rows (``scope = 'items'``).
* ``wip_adjustments``: write-on / write-off requests with their approval workflow.
* ``wip_transfers``: WIP value moved between two jobs of one client.
* ``credit_note_wip``: what was done with the WIP of a credit note, once per credit note.

Revision ID: 028
Revises: 027
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "028"
down_revision: str | None = "027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMPONENTS = "'time', 'fixed_fee', 'expense'"


def _organization_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="CASCADE",
    )


def _member_fk(column: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id", column],
        ["organization_members.organization_id", "organization_members.user_id"],
        name=name,
    )


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "wip_movements",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "approval_state", sa.Text(), server_default="not_required", nullable=False
        ),
        sa.Column("approved_by", sa.UUID(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversal_of_id", sa.UUID(), nullable=True),
        sa.Column("source_type", sa.Text(), nullable=True),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("item_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "item_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('write_on', 'write_off', 'transfer_out', 'transfer_in', "
            "'credit_return', 'credit_write_off', 'reversal')",
            name=op.f("ck_wip_movements_kind"),
        ),
        sa.CheckConstraint("scope IN ('amount', 'items')", name=op.f("ck_wip_movements_scope")),
        sa.CheckConstraint(
            f"component IN ({COMPONENTS})", name=op.f("ck_wip_movements_component")
        ),
        sa.CheckConstraint("amount <> 0", name=op.f("ck_wip_movements_amount_not_zero")),
        sa.CheckConstraint(
            "kind = 'reversal' "
            "OR (kind IN ('write_on', 'transfer_in', 'credit_return') AND amount > 0) "
            "OR (kind IN ('write_off', 'transfer_out', 'credit_write_off') AND amount < 0)",
            name=op.f("ck_wip_movements_amount_sign"),
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) BETWEEN 1 AND 500", name=op.f("ck_wip_movements_reason_length")
        ),
        sa.CheckConstraint(
            "(kind = 'reversal') = (reversal_of_id IS NOT NULL)",
            name=op.f("ck_wip_movements_reversal_of"),
        ),
        sa.CheckConstraint(
            "(approval_state = 'approved') = (approved_by IS NOT NULL)",
            name=op.f("ck_wip_movements_approval_fields"),
        ),
        sa.CheckConstraint(
            "approval_state IN ('not_required', 'approved')",
            name=op.f("ck_wip_movements_approval_state"),
        ),
        sa.CheckConstraint(
            "(source_type IS NULL) = (source_id IS NULL) "
            "AND (source_type IS NULL OR source_type IN "
            "('adjustment', 'transfer', 'write_off', 'credit_note'))",
            name=op.f("ck_wip_movements_source"),
        ),
        sa.CheckConstraint("item_count >= 0", name=op.f("ck_wip_movements_item_count")),
        sa.CheckConstraint(
            "jsonb_typeof(item_refs) = 'array'", name=op.f("ck_wip_movements_item_refs_array")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "reversal_of_id"],
            ["wip_movements.organization_id", "wip_movements.id"],
            name="fk_wip_movements_organization_id_reversal_of_id_wip_movements",
        ),
        _organization_fk("wip_movements"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wip_movements")),
        sa.UniqueConstraint("organization_id", "id", name="uq_wip_movements_organization_id_id"),
    )
    op.create_index(
        "uq_wip_movements_reversal_of_id",
        "wip_movements",
        ["reversal_of_id"],
        unique=True,
        postgresql_where=sa.text("reversal_of_id IS NOT NULL"),
    )
    op.create_index(
        "ix_wip_movements_organization_id_created_at",
        "wip_movements",
        ["organization_id", "created_at", "id"],
    )
    op.create_index(
        "ix_wip_movements_organization_id_effective_date",
        "wip_movements",
        ["organization_id", "effective_date", "id"],
    )
    op.create_index(
        "ix_wip_movements_organization_id_job_id", "wip_movements", ["organization_id", "job_id"]
    )
    op.create_index(
        "ix_wip_movements_organization_id_client_id",
        "wip_movements",
        ["organization_id", "client_id"],
    )
    op.create_index(
        "ix_wip_movements_organization_id_source",
        "wip_movements",
        ["organization_id", "source_type", "source_id"],
    )
    # The ledger is append-only. A practice's rows go with the practice: the foreign key's cascade
    # deletes them one trigger level deeper than a DELETE that somebody typed.
    op.execute(
        """
        CREATE FUNCTION prevent_wip_history_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_wip_movements_append_only BEFORE UPDATE OR DELETE "
        "ON wip_movements FOR EACH ROW EXECUTE FUNCTION prevent_wip_history_change()"
    )

    op.create_table(
        "wip_adjustments",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("threshold", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.UUID(), nullable=False),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("movement_id", sa.UUID(), nullable=True),
        sa.Column("reversal_movement_id", sa.UUID(), nullable=True),
        sa.Column("reversed_by", sa.UUID(), nullable=True),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversal_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('write_on', 'write_off')", name=op.f("ck_wip_adjustments_kind")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'reversed')",
            name=op.f("ck_wip_adjustments_status"),
        ),
        sa.CheckConstraint(
            f"component IN ({COMPONENTS})", name=op.f("ck_wip_adjustments_component")
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_wip_adjustments_amount")),
        sa.CheckConstraint(
            "length(btrim(reason)) BETWEEN 1 AND 500",
            name=op.f("ck_wip_adjustments_reason_length"),
        ),
        sa.CheckConstraint(
            "decision_note IS NULL OR length(btrim(decision_note)) BETWEEN 1 AND 500",
            name=op.f("ck_wip_adjustments_decision_note_length"),
        ),
        sa.CheckConstraint(
            "reversal_reason IS NULL OR length(btrim(reversal_reason)) BETWEEN 1 AND 500",
            name=op.f("ck_wip_adjustments_reversal_reason_length"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND movement_id IS NULL AND decided_by IS NULL "
            "AND reversed_by IS NULL) "
            "OR (status = 'approved' AND movement_id IS NOT NULL AND reversed_by IS NULL "
            "AND (decided_by IS NOT NULL) = approval_required) "
            "OR (status = 'rejected' AND movement_id IS NULL AND decided_by IS NOT NULL "
            "AND decision_note IS NOT NULL AND reversed_by IS NULL) "
            "OR (status = 'reversed' AND movement_id IS NOT NULL "
            "AND reversal_movement_id IS NOT NULL AND reversed_by IS NOT NULL "
            "AND reversal_reason IS NOT NULL)",
            name=op.f("ck_wip_adjustments_status_fields"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_wip_adjustments_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_wip_adjustments_organization_id_client_id_job_id_jobs",
        ),
        _member_fk("requested_by", "fk_wip_adjustments_organization_id_requested_by_members"),
        _member_fk("decided_by", "fk_wip_adjustments_organization_id_decided_by_members"),
        _member_fk("reversed_by", "fk_wip_adjustments_organization_id_reversed_by_members"),
        _organization_fk("wip_adjustments"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wip_adjustments")),
        sa.UniqueConstraint("organization_id", "id", name="uq_wip_adjustments_organization_id_id"),
    )
    op.create_index(
        "ix_wip_adjustments_organization_id_created_at",
        "wip_adjustments",
        ["organization_id", "created_at", "id"],
    )
    op.create_index(
        "ix_wip_adjustments_organization_id_client_id",
        "wip_adjustments",
        ["organization_id", "client_id"],
    )
    op.create_index(
        "ix_wip_adjustments_pending",
        "wip_adjustments",
        ["organization_id", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    add_updated_at_triggers("wip_adjustments")

    op.create_table(
        "wip_transfers",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("from_job_id", sa.UUID(), nullable=False),
        sa.Column("to_job_id", sa.UUID(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "items",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="applied", nullable=False),
        sa.Column("out_movement_id", sa.UUID(), nullable=False),
        sa.Column("in_movement_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("reversed_by", sa.UUID(), nullable=True),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversal_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("from_job_id <> to_job_id", name=op.f("ck_wip_transfers_different_jobs")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_wip_transfers_amount")),
        sa.CheckConstraint(
            f"component IN ({COMPONENTS})", name=op.f("ck_wip_transfers_component")
        ),
        sa.CheckConstraint(
            "status IN ('applied', 'reversed')", name=op.f("ck_wip_transfers_status")
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) BETWEEN 1 AND 500", name=op.f("ck_wip_transfers_reason_length")
        ),
        sa.CheckConstraint(
            "reversal_reason IS NULL OR length(btrim(reversal_reason)) BETWEEN 1 AND 500",
            name=op.f("ck_wip_transfers_reversal_reason_length"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(items) = 'array'", name=op.f("ck_wip_transfers_items_array")
        ),
        sa.CheckConstraint(
            "(status = 'applied' AND reversed_by IS NULL AND reversed_at IS NULL "
            "AND reversal_reason IS NULL) "
            "OR (status = 'reversed' AND reversed_by IS NOT NULL AND reversed_at IS NOT NULL "
            "AND reversal_reason IS NOT NULL)",
            name=op.f("ck_wip_transfers_status_fields"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_wip_transfers_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "from_job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_wip_transfers_organization_id_client_id_from_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "to_job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_wip_transfers_organization_id_client_id_to_job_id_jobs",
        ),
        _member_fk("created_by", "fk_wip_transfers_organization_id_created_by_members"),
        _member_fk("reversed_by", "fk_wip_transfers_organization_id_reversed_by_members"),
        _organization_fk("wip_transfers"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wip_transfers")),
        sa.UniqueConstraint("organization_id", "id", name="uq_wip_transfers_organization_id_id"),
    )
    op.create_index(
        "ix_wip_transfers_organization_id_created_at",
        "wip_transfers",
        ["organization_id", "created_at", "id"],
    )
    op.create_index(
        "ix_wip_transfers_organization_id_client_id",
        "wip_transfers",
        ["organization_id", "client_id"],
    )
    add_updated_at_triggers("wip_transfers")

    op.create_table(
        "credit_note_wip",
        sa.Column("credit_note_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("net_credited", sa.BigInteger(), nullable=False),
        sa.Column("wip_value", sa.BigInteger(), nullable=False),
        sa.Column("item_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("mode IN ('return', 'write_off')", name=op.f("ck_credit_note_wip_mode")),
        sa.CheckConstraint(
            "length(btrim(reason)) BETWEEN 1 AND 500", name=op.f("ck_credit_note_wip_reason_length")
        ),
        sa.CheckConstraint(
            "net_credited > 0 AND wip_value >= 0 AND item_count >= 0",
            name=op.f("ck_credit_note_wip_values"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "credit_note_id"],
            ["credit_notes.organization_id", "credit_notes.id"],
            name="fk_credit_note_wip_organization_id_credit_note_id_credit_notes",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_credit_note_wip_organization_id_client_id_clients",
        ),
        _member_fk("created_by", "fk_credit_note_wip_organization_id_created_by_members"),
        _organization_fk("credit_note_wip"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_note_wip")),
        sa.UniqueConstraint(
            "organization_id",
            "credit_note_id",
            name="uq_credit_note_wip_organization_id_credit_note_id",
        ),
    )
    add_updated_at_triggers("credit_note_wip")

    # The write-offs made before the ledger existed, as memo rows (one per job and component).
    op.execute(
        """
        INSERT INTO wip_movements
            (id, organization_id, kind, scope, component, client_id, job_id, amount,
             effective_date, reason, actor_user_id, source_type, source_id, item_count, created_at)
        SELECT gen_random_uuid(), w.organization_id, 'write_off', 'items', x.component,
               w.client_id, x.job_id, -SUM(x.value)::bigint, w.written_off_on, w.reason,
               w.created_by, 'write_off', w.id, COUNT(*)::int, w.created_at
        FROM wip_write_offs w
        JOIN (
            SELECT a.organization_id, a.write_off_id, t.job_id,
                   CASE WHEN j.billing_basis = 'fixed' THEN 'fixed_fee' ELSE 'time' END
                       AS component,
                   a.wip_value AS value
            FROM wip_allocations a
            JOIN time_entries t
                ON t.organization_id = a.organization_id AND t.id = a.time_entry_id
            LEFT JOIN jobs j ON j.organization_id = t.organization_id AND j.id = t.job_id
            WHERE a.write_off_id IS NOT NULL
            UNION ALL
            SELECT a.organization_id, a.write_off_id, d.job_id, 'expense' AS component,
                   a.wip_value AS value
            FROM wip_allocations a
            JOIN disbursements d
                ON d.organization_id = a.organization_id AND d.id = a.disbursement_id
            WHERE a.write_off_id IS NOT NULL
        ) x ON x.organization_id = w.organization_id AND x.write_off_id = w.id
        GROUP BY w.organization_id, w.id, w.client_id, w.written_off_on, w.reason, w.created_by,
                 w.created_at, x.job_id, x.component
        HAVING SUM(x.value) > 0
        """
    )


def downgrade() -> None:
    op.drop_table("credit_note_wip")
    op.drop_table("wip_transfers")
    op.drop_table("wip_adjustments")
    op.drop_table("wip_movements")
    op.execute("DROP FUNCTION prevent_wip_history_change()")

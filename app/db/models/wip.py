"""Work in progress beyond invoices: the movement ledger, adjustments, transfers, credit-note WIP
and the frozen as-at snapshots of historic WIP.

* ``wip_movements``: the **immutable ledger**. A row is written when a movement takes effect and
  is never changed or deleted (a trigger refuses it); a correction is a new ``reversal`` row that
  points at the original (``reversal_of_id``). ``scope = 'amount'`` movements adjust the WIP
  *balance* of a job (write-on, write-off by amount, transfer in/out); ``scope = 'items'``
  movements are memos of a change that already happened to the items themselves (an item
  write-off, a credit-note return).
  The table keeps client and job as plain ids, without foreign keys, like the other history tables.
* ``wip_adjustments``: a write-on or write-off request and its workflow (pending, approved,
  rejected, reversed). Only an effective adjustment has a ledger row.
* ``wip_transfers``: a move of WIP value between two jobs of the same client: two ledger rows.
* ``credit_note_wip``: what was done with the WIP of a credit note, once per credit note.
* ``wip_snapshots`` / ``wip_snapshot_items``: the WIP of a closed day as it was, frozen the first
  time it is reported (append-only, so a later invoice, write-off or rate change never alters it).

Every table carries ``organization_id``; the tables that are not history also carry composite keys
so a row can never point into another practice.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin

WIP_COMPONENTS = ("time", "fixed_fee", "expense")
MOVEMENT_KINDS = (
    "write_on",
    "write_off",
    "transfer_out",
    "transfer_in",
    "credit_return",
    "credit_write_off",
    "reversal",
)
MOVEMENT_SCOPES = ("amount", "items")
MOVEMENT_SOURCES = ("adjustment", "transfer", "write_off", "credit_note")
ADJUSTMENT_KINDS = ("write_on", "write_off")
ADJUSTMENT_STATUSES = ("pending", "approved", "rejected", "reversed")
TRANSFER_STATUSES = ("applied", "reversed")
CREDIT_WIP_MODES = ("return", "write_off")
SNAPSHOT_SOURCES = ("request", "scheduled")
SNAPSHOT_KINDS = ("time", "fixed_fee", "expense", "disbursement", "adjustment")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _member_fk(column: str, name: str) -> ForeignKeyConstraint:
    """The person must belong to the practice."""
    return ForeignKeyConstraint(
        ["organization_id", column],
        ["organization_members.organization_id", "organization_members.user_id"],
        name=name,
    )


class WipMovement(Base, IdMixin, OrgScopedMixin):
    """One entry of the WIP ledger. Append-only."""

    __tablename__ = "wip_movements"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_wip_movements_organization_id_id"),
        ForeignKeyConstraint(
            ["organization_id", "reversal_of_id"],
            ["wip_movements.organization_id", "wip_movements.id"],
            name="fk_wip_movements_organization_id_reversal_of_id_wip_movements",
        ),
        CheckConstraint(_in("kind", MOVEMENT_KINDS), name="kind"),
        CheckConstraint(_in("scope", MOVEMENT_SCOPES), name="scope"),
        CheckConstraint(_in("component", WIP_COMPONENTS), name="component"),
        CheckConstraint("amount <> 0", name="amount_not_zero"),
        CheckConstraint(
            "kind = 'reversal' "
            "OR (kind IN ('write_on', 'transfer_in', 'credit_return') AND amount > 0) "
            "OR (kind IN ('write_off', 'transfer_out', 'credit_write_off') AND amount < 0)",
            name="amount_sign",
        ),
        CheckConstraint("length(btrim(reason)) BETWEEN 1 AND 500", name="reason_length"),
        CheckConstraint("(kind = 'reversal') = (reversal_of_id IS NOT NULL)", name="reversal_of"),
        CheckConstraint(
            "(approval_state = 'approved') = (approved_by IS NOT NULL)", name="approval_fields"
        ),
        CheckConstraint(
            "approval_state IN ('not_required', 'approved')", name="approval_state"
        ),
        CheckConstraint(
            "(source_type IS NULL) = (source_id IS NULL) "
            "AND (source_type IS NULL OR " + _in("source_type", MOVEMENT_SOURCES) + ")",
            name="source",
        ),
        CheckConstraint("item_count >= 0", name="item_count"),
        CheckConstraint("jsonb_typeof(item_refs) = 'array'", name="item_refs_array"),
        # A movement is reversed at most once.
        Index(
            "uq_wip_movements_reversal_of_id",
            "reversal_of_id",
            unique=True,
            postgresql_where=text("reversal_of_id IS NOT NULL"),
        ),
        Index("ix_wip_movements_organization_id_created_at", "organization_id", "created_at", "id"),
        Index(
            "ix_wip_movements_organization_id_effective_date",
            "organization_id",
            "effective_date",
            "id",
        ),
        Index("ix_wip_movements_organization_id_job_id", "organization_id", "job_id"),
        Index("ix_wip_movements_organization_id_client_id", "organization_id", "client_id"),
        Index(
            "ix_wip_movements_organization_id_source",
            "organization_id",
            "source_type",
            "source_id",
        ),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # ``amount``: adjusts the WIP balance of the job. ``items``: a memo of a change already made
    # to the items (they left WIP, or came back); it never counts in the balance.
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    component: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Signed pence: positive adds WIP value, negative removes it.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approval_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="not_required"
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_of_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_type: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    item_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class WipAdjustment(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A write-on or write-off of an amount on a job (or a client), with its approval workflow."""

    __tablename__ = "wip_adjustments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_wip_adjustments_organization_id_id"),
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_wip_adjustments_organization_id_client_id_clients",
        ),
        ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_wip_adjustments_organization_id_client_id_job_id_jobs",
        ),
        _member_fk("requested_by", "fk_wip_adjustments_organization_id_requested_by_members"),
        _member_fk("decided_by", "fk_wip_adjustments_organization_id_decided_by_members"),
        _member_fk("reversed_by", "fk_wip_adjustments_organization_id_reversed_by_members"),
        CheckConstraint(_in("kind", ADJUSTMENT_KINDS), name="kind"),
        CheckConstraint(_in("status", ADJUSTMENT_STATUSES), name="status"),
        CheckConstraint(_in("component", WIP_COMPONENTS), name="component"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("length(btrim(reason)) BETWEEN 1 AND 500", name="reason_length"),
        CheckConstraint(
            "decision_note IS NULL OR length(btrim(decision_note)) BETWEEN 1 AND 500",
            name="decision_note_length",
        ),
        CheckConstraint(
            "reversal_reason IS NULL OR length(btrim(reversal_reason)) BETWEEN 1 AND 500",
            name="reversal_reason_length",
        ),
        CheckConstraint(
            "(status = 'pending' AND movement_id IS NULL AND decided_by IS NULL "
            "AND reversed_by IS NULL) "
            "OR (status = 'approved' AND movement_id IS NOT NULL AND reversed_by IS NULL "
            "AND (decided_by IS NOT NULL) = approval_required) "
            "OR (status = 'rejected' AND movement_id IS NULL AND decided_by IS NOT NULL "
            "AND decision_note IS NOT NULL AND reversed_by IS NULL) "
            "OR (status = 'reversed' AND movement_id IS NOT NULL "
            "AND reversal_movement_id IS NOT NULL AND reversed_by IS NOT NULL "
            "AND reversal_reason IS NOT NULL)",
            name="status_fields",
        ),
        Index(
            "ix_wip_adjustments_organization_id_created_at", "organization_id", "created_at", "id"
        ),
        Index("ix_wip_adjustments_organization_id_client_id", "organization_id", "client_id"),
        Index(
            "ix_wip_adjustments_pending",
            "organization_id",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    component: Mapped[str] = mapped_column(Text, nullable=False)
    # Always positive: ``kind`` says whether it adds or removes WIP.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    # Whether the amount was above the practice's threshold when it was requested, and the
    # threshold that applied then.
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    threshold: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    # The ledger rows (no foreign key: the ledger is history).
    movement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reversal_movement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reversed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_reason: Mapped[str | None] = mapped_column(Text)


class WipTransfer(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """WIP value moved from one job of a client to another job of the same client."""

    __tablename__ = "wip_transfers"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_wip_transfers_organization_id_id"),
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_wip_transfers_organization_id_client_id_clients",
        ),
        # Both jobs must be the client's: the same client id in both keys.
        ForeignKeyConstraint(
            ["organization_id", "client_id", "from_job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_wip_transfers_organization_id_client_id_from_job_id_jobs",
        ),
        ForeignKeyConstraint(
            ["organization_id", "client_id", "to_job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_wip_transfers_organization_id_client_id_to_job_id_jobs",
        ),
        _member_fk("created_by", "fk_wip_transfers_organization_id_created_by_members"),
        _member_fk("reversed_by", "fk_wip_transfers_organization_id_reversed_by_members"),
        CheckConstraint("from_job_id <> to_job_id", name="different_jobs"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint(_in("component", WIP_COMPONENTS), name="component"),
        CheckConstraint(_in("status", TRANSFER_STATUSES), name="status"),
        CheckConstraint("length(btrim(reason)) BETWEEN 1 AND 500", name="reason_length"),
        CheckConstraint(
            "reversal_reason IS NULL OR length(btrim(reversal_reason)) BETWEEN 1 AND 500",
            name="reversal_reason_length",
        ),
        CheckConstraint("jsonb_typeof(items) = 'array'", name="items_array"),
        CheckConstraint(
            "(status = 'applied' AND reversed_by IS NULL AND reversed_at IS NULL "
            "AND reversal_reason IS NULL) "
            "OR (status = 'reversed' AND reversed_by IS NOT NULL AND reversed_at IS NOT NULL "
            "AND reversal_reason IS NOT NULL)",
            name="status_fields",
        ),
        Index("ix_wip_transfers_organization_id_created_at", "organization_id", "created_at", "id"),
        Index("ix_wip_transfers_organization_id_client_id", "organization_id", "client_id"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    from_job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    to_job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    component: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # The items the person chose, as ``[{kind, id, value}]`` (empty when an amount was given).
    items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="applied")
    out_movement_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    in_movement_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reversed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_reason: Mapped[str | None] = mapped_column(Text)


class CreditNoteWip(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """What happened to the WIP of a credit note: returned to WIP, or written off. Once."""

    __tablename__ = "credit_note_wip"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "credit_note_id",
            name="uq_credit_note_wip_organization_id_credit_note_id",
        ),
        ForeignKeyConstraint(
            ["organization_id", "credit_note_id"],
            ["credit_notes.organization_id", "credit_notes.id"],
            name="fk_credit_note_wip_organization_id_credit_note_id_credit_notes",
        ),
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_credit_note_wip_organization_id_client_id_clients",
        ),
        _member_fk("created_by", "fk_credit_note_wip_organization_id_created_by_members"),
        CheckConstraint(_in("mode", CREDIT_WIP_MODES), name="mode"),
        CheckConstraint("length(btrim(reason)) BETWEEN 1 AND 500", name="reason_length"),
        CheckConstraint("net_credited > 0 AND wip_value >= 0 AND item_count >= 0", name="values"),
    )

    credit_note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # The credit note's net total.
    net_credited: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The WIP value that came back (return) or was written off (write_off).
    wip_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class WipSnapshot(Base, IdMixin, OrgScopedMixin):
    """The WIP of one closed day, frozen the first time it was reported. Append-only."""

    __tablename__ = "wip_snapshots"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_wip_snapshots_organization_id_id"),
        UniqueConstraint(
            "organization_id", "as_at", name="uq_wip_snapshots_organization_id_as_at"
        ),
        CheckConstraint(_in("source", SNAPSHOT_SOURCES), name="source"),
        CheckConstraint(
            "fixed_fee_wip_mode IN ('on_completion', 'by_budget_progress')",
            name="fixed_fee_wip_mode",
        ),
        CheckConstraint("item_count >= 0", name="item_count"),
    )

    as_at: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="request")
    # The practice settings the figures were produced under.
    fixed_fee_wip_mode: Mapped[str] = mapped_column(Text, nullable=False)
    require_timesheet_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    taken_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    taken_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class WipSnapshotItem(Base, IdMixin, OrgScopedMixin):
    """One item of a snapshot, with the client, job and people as they were. Append-only."""

    __tablename__ = "wip_snapshot_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "snapshot_id"],
            ["wip_snapshots.organization_id", "wip_snapshots.id"],
            name="fk_wip_snapshot_items_snapshot_id_wip_snapshots",
            ondelete="CASCADE",
        ),
        CheckConstraint(_in("kind", SNAPSHOT_KINDS), name="kind"),
        CheckConstraint(_in("component", WIP_COMPONENTS), name="component"),
        Index(
            "ix_wip_snapshot_items_snapshot_id_client_id_job_id",
            "snapshot_id",
            "client_id",
            "job_id",
        ),
        Index("ix_wip_snapshot_items_snapshot_id_item_date", "snapshot_id", "item_date", "id"),
    )

    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    component: Mapped[str] = mapped_column(Text, nullable=False)
    # The time entry, disbursement or expense line; the job for a fixed fee; the movement for an
    # adjustment.
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    client_code: Mapped[str] = mapped_column(Text, nullable=False)
    client_name: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_number: Mapped[str | None] = mapped_column(Text)
    job_name: Mapped[str | None] = mapped_column(Text)
    billing_basis: Mapped[str | None] = mapped_column(Text)
    partner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    person_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    item_date: Mapped[date] = mapped_column(Date, nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charge_value: Mapped[int | None] = mapped_column(BigInteger)
    description: Mapped[str | None] = mapped_column(Text)


__all__ = [
    "ADJUSTMENT_KINDS",
    "ADJUSTMENT_STATUSES",
    "CREDIT_WIP_MODES",
    "MOVEMENT_KINDS",
    "MOVEMENT_SCOPES",
    "MOVEMENT_SOURCES",
    "SNAPSHOT_KINDS",
    "SNAPSHOT_SOURCES",
    "TRANSFER_STATUSES",
    "WIP_COMPONENTS",
    "CreditNoteWip",
    "WipAdjustment",
    "WipMovement",
    "WipSnapshot",
    "WipSnapshotItem",
    "WipTransfer",
]

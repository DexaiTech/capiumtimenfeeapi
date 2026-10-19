"""Expenses: the practice's expense types, staff expense claims with their lines, the history of
decisions on a claim and the receipts of a line.

* ``expense_types``: what a line is for (Travel, Mileage, ...). A mileage type prices a line from
  the miles and the practice's mileage rate. A used type is deactivated, never deleted.
* ``expense_claims``: one person's claim. ``draft -> submitted -> approved``, or ``submitted ->
  returned -> (edit) -> submitted``; a CHECK keeps the decision columns in step with the status.
  Totals are never stored: they are the sum of the lines.
* ``expenses``: the lines of a claim. Money is pence and ``gross = net + vat`` is a CHECK. The VAT
  rate, the mileage rate and the markup in force when the line was saved are kept on it, so a
  later change of a setting never rewrites it. An approved, billable line with a recharge amount
  and ``billing_status = 'unbilled'`` is work in progress (docs/modules/expenses.md).
* ``expense_claim_events``: one row for every submission, recall, approval, return and correction.
* ``expense_receipts``: the files (PDF, JPEG, PNG) that prove a line; the bytes live in the file
  storage under ``storage_key`` (random, never derived from the name).

Every reference between these tables carries ``organization_id`` (composite keys), so a row can
never point into another practice.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin

EXPENSE_CLAIM_STATUSES = ("draft", "submitted", "approved", "returned")
EXPENSE_PAYERS = ("staff", "practice")
EXPENSE_BILLING_STATUSES = ("unbilled", "in_draft", "billed", "written_off")
CLAIM_EVENT_ACTIONS = ("submitted", "recalled", "approved", "returned", "corrected")
MAX_RECEIPT_BYTES = 10 * 1024 * 1024
MAX_RECEIPTS_PER_LINE = 5


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _member_fk(column: str, name: str) -> ForeignKeyConstraint:
    """The person must belong to the practice."""
    return ForeignKeyConstraint(
        ["organization_id", column],
        ["organization_members.organization_id", "organization_members.user_id"],
        name=name,
    )


class ExpenseType(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "expense_types"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_expense_types_organization_id_id"),
        ForeignKeyConstraint(
            ["organization_id", "default_vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_expense_types_default_vat_rate_id_vat_rates",
        ),
        CheckConstraint("length(name) BETWEEN 1 AND 80", name="name_length"),
        CheckConstraint("code ~ '^[A-Z0-9_-]{1,12}$'", name="code_format"),
        # The name is unique among the active types: a retired name can be used again.
        Index(
            "uq_expense_types_organization_id_name_lower",
            "organization_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index("uq_expense_types_organization_id_code", "organization_id", "code", unique=True),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    # The VAT code a line starts with when only its net amount is given; null = no VAT.
    default_vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # A mileage line is priced from its miles and the practice's mileage rate.
    is_mileage: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    default_billable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class ExpenseClaim(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "expense_claims"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_expense_claims_organization_id_id"),
        _member_fk("user_id", "fk_expense_claims_organization_id_user_members"),
        _member_fk("created_by", "fk_expense_claims_organization_id_created_by_members"),
        _member_fk("decided_by", "fk_expense_claims_organization_id_decided_by_members"),
        CheckConstraint(_in("status", EXPENSE_CLAIM_STATUSES), name="status"),
        CheckConstraint("title IS NULL OR length(title) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint(
            "decision_note IS NULL OR length(decision_note) BETWEEN 1 AND 2000",
            name="decision_note_length",
        ),
        CheckConstraint(
            "(status = 'draft' AND submitted_at IS NULL AND decided_at IS NULL "
            "AND decided_by IS NULL) "
            "OR (status = 'submitted' AND submitted_at IS NOT NULL AND decided_at IS NULL "
            "AND decided_by IS NULL) "
            "OR (status IN ('approved', 'returned') AND submitted_at IS NOT NULL "
            "AND decided_at IS NOT NULL AND decided_by IS NOT NULL)",
            name="status_fields",
        ),
        CheckConstraint("status <> 'returned' OR decision_note IS NOT NULL", name="return_note"),
        Index(
            "ix_expense_claims_organization_id_user_id_created_at",
            "organization_id",
            "user_id",
            "created_at",
        ),
        Index(
            "ix_expense_claims_organization_id_created_at", "organization_id", "created_at", "id"
        ),
        # The approval queue: submitted claims, oldest first.
        Index(
            "ix_expense_claims_submitted",
            "organization_id",
            "submitted_at",
            postgresql_where=text("status = 'submitted'"),
        ),
    )

    # The person the claim is for: a member, possibly one without a login (recorded on their
    # behalf by someone with ``expense.manage``).
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Whoever entered it: the claimant, or the person who recorded it on their behalf.
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # The reason a claim was returned (required then); an optional note when approved.
    decision_note: Mapped[str | None] = mapped_column(Text)


class Expense(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """One line of an expense claim."""

    __tablename__ = "expenses"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_expenses_organization_id_id"),
        ForeignKeyConstraint(
            ["organization_id", "claim_id"],
            ["expense_claims.organization_id", "expense_claims.id"],
            name="fk_expenses_organization_id_claim_id_expense_claims",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "expense_type_id"],
            ["expense_types.organization_id", "expense_types.id"],
            name="fk_expenses_organization_id_expense_type_id_expense_types",
        ),
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_expenses_organization_id_client_id_clients",
        ),
        # The job must belong to the line's client.
        ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_expenses_organization_id_client_id_job_id_jobs",
        ),
        ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_expenses_organization_id_vat_rate_id_vat_rates",
        ),
        CheckConstraint("length(description) BETWEEN 1 AND 500", name="description_length"),
        CheckConstraint(
            "supplier IS NULL OR length(supplier) BETWEEN 1 AND 200", name="supplier_length"
        ),
        CheckConstraint("net >= 0 AND vat >= 0", name="amounts"),
        CheckConstraint("gross = net + vat", name="gross_total"),
        CheckConstraint(
            "vat_rate_bp IS NULL OR vat_rate_bp BETWEEN 0 AND 10000", name="vat_rate_bp"
        ),
        CheckConstraint("(vat_rate_id IS NULL) = (vat_rate_bp IS NULL)", name="vat_snapshot"),
        CheckConstraint("miles IS NULL OR miles > 0", name="miles"),
        CheckConstraint(
            "(miles IS NULL) = (mileage_rate_applied IS NULL) "
            "AND (mileage_rate_applied IS NULL OR mileage_rate_applied >= 0)",
            name="mileage_snapshot",
        ),
        CheckConstraint("markup_bp BETWEEN 0 AND 100000", name="markup_bp"),
        CheckConstraint("recharge_amount >= 0", name="recharge_amount"),
        CheckConstraint("is_billable OR recharge_amount = 0", name="recharge_needs_billable"),
        CheckConstraint("NOT is_billable OR client_id IS NOT NULL", name="billable_needs_client"),
        CheckConstraint("job_id IS NULL OR client_id IS NOT NULL", name="job_needs_client"),
        CheckConstraint(_in("payer", EXPENSE_PAYERS), name="payer"),
        CheckConstraint(_in("billing_status", EXPENSE_BILLING_STATUSES), name="billing_status"),
        CheckConstraint("is_billable OR billing_status = 'unbilled'", name="non_billable_status"),
        Index("ix_expenses_organization_id_claim_id", "organization_id", "claim_id"),
        Index(
            "ix_expenses_organization_id_client_id_incurred_on",
            "organization_id",
            "client_id",
            "incurred_on",
        ),
        Index("ix_expenses_organization_id_incurred_on", "organization_id", "incurred_on", "id"),
        Index(
            "ix_expenses_organization_id_job_id",
            "organization_id",
            "job_id",
            postgresql_where=text("job_id IS NOT NULL"),
        ),
        Index("ix_expenses_organization_id_expense_type_id", "organization_id", "expense_type_id"),
        # The WIP candidates: billable, unbilled, with something to recharge (the claim must
        # also be approved, which the query checks on the claim).
        Index(
            "ix_expenses_wip",
            "organization_id",
            "client_id",
            "incurred_on",
            postgresql_where=text(
                "is_billable AND billing_status = 'unbilled' AND recharge_amount > 0"
            ),
        ),
    )

    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expense_type_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    incurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    supplier: Mapped[str | None] = mapped_column(Text)
    # The VAT code of the cost and the rate it had when the line was saved (both or neither).
    vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    vat_rate_bp: Mapped[int | None] = mapped_column(Integer)
    net: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vat: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    gross: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Mileage types only: net = miles x the practice's rate (pence per mile) at the time.
    miles: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    mileage_rate_applied: Mapped[int | None] = mapped_column(BigInteger)
    is_billable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # The practice's markup when the line was saved, and what the client is charged for it.
    markup_bp: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    recharge_amount: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # Who paid: the staff member (reimbursed) or the practice (company card).
    payer: Mapped[str] = mapped_column(Text, nullable=False, server_default="staff")
    billing_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="unbilled")


class ExpenseClaimEvent(Base, IdMixin, OrgScopedMixin):
    """What happened to a claim and who did it: the history of its decisions."""

    __tablename__ = "expense_claim_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "claim_id"],
            ["expense_claims.organization_id", "expense_claims.id"],
            name="fk_expense_claim_events_claim_id_expense_claims",
            ondelete="CASCADE",
        ),
        _member_fk("actor_user_id", "fk_expense_claim_events_organization_id_actor_members"),
        CheckConstraint(_in("action", CLAIM_EVENT_ACTIONS), name="action"),
        CheckConstraint("note IS NULL OR length(note) BETWEEN 1 AND 2000", name="note_length"),
        Index(
            "ix_expense_claim_events_organization_id_claim_id_at",
            "organization_id",
            "claim_id",
            "created_at",
        ),
    )

    claim_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExpenseReceipt(Base, IdMixin, OrgScopedMixin):
    __tablename__ = "expense_receipts"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_expense_receipts_organization_id_id"),
        ForeignKeyConstraint(
            ["organization_id", "expense_id"],
            ["expenses.organization_id", "expenses.id"],
            name="fk_expense_receipts_expense_id_expenses",
            ondelete="CASCADE",
        ),
        _member_fk("uploaded_by", "fk_expense_receipts_organization_id_uploader_members"),
        CheckConstraint("length(file_name) BETWEEN 1 AND 200", name="file_name_length"),
        CheckConstraint("length(content_type) BETWEEN 3 AND 100", name="content_type_length"),
        CheckConstraint(f"size_bytes BETWEEN 1 AND {MAX_RECEIPT_BYTES}", name="size_bytes"),
        CheckConstraint("length(storage_key) BETWEEN 16 AND 128", name="storage_key_length"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        Index("uq_expense_receipts_storage_key", "storage_key", unique=True),
        Index("ix_expense_receipts_organization_id_expense_id", "organization_id", "expense_id"),
    )

    expense_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The sanitised name the person gave the file.
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    # What the content really is (sniffed), never what the client claimed.
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "CLAIM_EVENT_ACTIONS",
    "EXPENSE_BILLING_STATUSES",
    "EXPENSE_CLAIM_STATUSES",
    "EXPENSE_PAYERS",
    "MAX_RECEIPTS_PER_LINE",
    "MAX_RECEIPT_BYTES",
    "Expense",
    "ExpenseClaim",
    "ExpenseClaimEvent",
    "ExpenseReceipt",
    "ExpenseType",
]

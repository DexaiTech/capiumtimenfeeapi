"""Debtors (who owes what, aged), payment reminders and their history."""

import uuid
from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.schemas.billing import ClientRef, PersonRef
from app.schemas.common import RequestModel

Bucket = Literal["current", "days_1_30", "days_31_60", "days_61_90", "days_90_plus"]
DebtorSort = Literal[
    "client",
    "total",
    "current",
    "days_1_30",
    "days_31_60",
    "days_61_90",
    "days_90_plus",
    "oldest_due",
]
SortOrder = Literal["asc", "desc"]
# What a person can choose when reminding about overdue invoices.
ManualReminderTemplate = Literal["client_payment_reminder", "client_payment_final_notice"]
# Every template a reminder in the log may have used (the scheduled job also sends the note
# before a due date).
ReminderTemplate = Literal[
    "client_payment_reminder", "client_payment_final_notice", "client_payment_upcoming"
]
SkipReason = Literal[
    "already_reminded_today", "not_overdue", "no_balance", "not_issued", "no_recipient"
]
ReminderStatus = Literal["queued", "sent", "failed", "skipped"]
ReminderSource = Literal["manual", "scheduled"]
OpenStatus = Literal["issued", "part_paid", "overdue"]


class DebtBuckets(BaseModel):
    """What is owed, by how far past its due date (as at the report date), in pence."""

    current: int = Field(description="Not past its due date (due on the date or later)")
    days_1_30: int = Field(description="1 to 30 days past due")
    days_31_60: int = Field(description="31 to 60 days past due")
    days_61_90: int = Field(description="61 to 90 days past due")
    days_90_plus: int = Field(description="91 days or more past due")


class DebtorTotals(DebtBuckets):
    total: int
    overdue: int = Field(description="`total - current`")
    client_count: int
    invoice_count: int


class DebtorRow(DebtBuckets):
    client: ClientRef
    partner_user_id: uuid.UUID | None
    manager_user_id: uuid.UUID | None
    total: int
    overdue: int = Field(description="`total - current`")
    invoice_count: int
    oldest_due_date: date | None = Field(description="The earliest due date of the open invoices")
    max_days_overdue: int = Field(description="Days past the oldest due date (0 when none is due)")
    last_reminder_at: datetime | None = Field(
        description="The latest payment reminder (queued or sent) about any of the invoices"
    )


class DebtorsRead(BaseModel):
    as_at: date
    currency: str
    totals: DebtorTotals = Field(description="Of every matching client, not only this page")
    rows: list[DebtorRow]


class DebtorBucketTotals(DebtBuckets):
    total: int


class DebtorInvoiceRead(BaseModel):
    id: uuid.UUID
    number: str | None
    source: str | None = Field(
        description="`opening` for an opening-balance invoice, otherwise null"
    )
    issue_date: date | None
    due_date: date | None
    days_overdue: int = Field(description="0 when not yet due")
    bucket: Bucket
    gross_total: int
    paid: int = Field(description="Payments received by the date, allocated to the invoice")
    credited: int = Field(description="Credit notes issued by the date")
    outstanding: int
    display_status: OpenStatus
    last_reminder_at: datetime | None
    reminder_count: int = Field(description="Reminders queued or sent about this invoice")


class DebtorClientInvoicesRead(BaseModel):
    client: ClientRef
    as_at: date
    totals: DebtorBucketTotals
    invoices: list[DebtorInvoiceRead] = Field(description="Open invoices, oldest due first")


# --- Reminders ---------------------------------------------------------------------------


class ReminderRequest(RequestModel):
    client_ids: list[uuid.UUID] | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Remind about every overdue invoice of these clients",
    )
    invoice_ids: list[uuid.UUID] | None = Field(
        default=None, min_length=1, max_length=500, description="Remind about these invoices"
    )
    template: ManualReminderTemplate = Field(
        default="client_payment_reminder",
        description="The email template: the reminder, or the firmer final notice",
    )
    dry_run: bool = Field(
        default=False, description="Show what would be sent; nothing is written or queued"
    )

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        if (self.client_ids is None) == (self.invoice_ids is None):
            raise ValueError("client_ids: Give exactly one of client_ids and invoice_ids")
        return self


class ReminderInvoice(BaseModel):
    invoice_id: uuid.UUID
    number: str | None
    due_date: date | None
    days_overdue: int
    outstanding: int


class ReminderClient(BaseModel):
    """The one email a client gets: its overdue invoices in a single message."""

    client: ClientRef
    to: list[str]
    subject: str
    text: str = Field(description="The plain-text body as it goes out")
    total_due: int
    invoices: list[ReminderInvoice]


class ReminderSkipped(BaseModel):
    invoice_id: uuid.UUID
    number: str | None
    client_id: uuid.UUID
    reason: SkipReason


class ReminderResult(BaseModel):
    dry_run: bool
    template: ReminderTemplate
    emails_queued: int = Field(description="Emails queued (0 for a dry run)")
    invoices_queued: int = Field(description="Invoices reminded (0 for a dry run)")
    clients: list[ReminderClient] = Field(description="What goes out (or would go out)")
    skipped: list[ReminderSkipped]


class ReminderInvoiceRef(BaseModel):
    id: uuid.UUID
    number: str | None


class ReminderRead(BaseModel):
    id: uuid.UUID
    client: ClientRef
    invoice: ReminderInvoiceRef
    template: str
    source: ReminderSource
    status: ReminderStatus
    sent_on: date = Field(description="The practice-local day it was queued for")
    to: list[str]
    days_overdue: int
    amount_due: int
    sent_by: PersonRef | None = Field(description="Null for the scheduled job")
    sent_at: datetime | None
    error: str | None
    created_at: datetime

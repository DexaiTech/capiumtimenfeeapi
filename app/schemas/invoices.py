"""Request and response models of invoices, invoice lines and credit notes."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.db.models import Client, CreditNote, CreditNoteLine, Invoice, InvoiceLine
from app.schemas.billing import ClientRef, PersonRef
from app.schemas.bulk import MAX_BULK_IDS
from app.schemas.common import RequestModel

SignedMoney = Annotated[
    int, Field(ge=-(10**12), le=10**12, description="Minor units (pence); negative = discount")
]
Money = Annotated[int, Field(ge=0, le=10**12, description="Minor units (pence)")]
PositiveMoney = Annotated[int, Field(gt=0, le=10**12, description="Minor units (pence)")]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
GroupBy = Literal["job", "entry", "single"]
DisplayStatus = Literal[
    "draft",
    "issued",
    "void",
    "overdue",
    "part_paid",
    "paid",
    "awaiting_approval",
    "approved",
    "credited",
]
ApprovalAction = Literal["submitted", "approved", "rejected"]
ItemIds = Annotated[list[uuid.UUID], Field(max_length=2000)]
# A field named ``date`` would shadow the type in the class body.
DateType = date


def _blank_is_null(value: str | None) -> str | None:
    return value or None


def _reject_nulls(model: BaseModel, *fields: str) -> None:
    for field in fields:
        if field in model.model_fields_set and getattr(model, field) is None:
            raise ValueError(f"{field}: This field cannot be null")


# --- Requests ---------------------------------------------------------------------------


class LineOverride(RequestModel):
    """Changes one line of the draft being built. The line is chosen by exactly one of:
    the job whose time it bills, a time entry it contains, or the disbursement it bills."""

    job_id: uuid.UUID | None = None
    time_entry_id: uuid.UUID | None = None
    disbursement_id: uuid.UUID | None = None
    net_amount: Money | None = Field(default=None, description="The bill amount of the line")
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    vat_rate_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one_selector(self) -> Self:
        chosen = [f for f in ("job_id", "time_entry_id", "disbursement_id") if getattr(self, f)]
        if len(chosen) != 1:
            raise ValueError("Give exactly one of job_id, time_entry_id and disbursement_id")
        return self


class WipSelection(RequestModel):
    time_entry_ids: ItemIds = Field(default_factory=list)
    disbursement_ids: ItemIds = Field(default_factory=list)
    group_by: GroupBy = Field(
        default="job",
        description="One line per job, one per time entry, or one line for all time",
    )
    overrides: list[LineOverride] = Field(default_factory=list, max_length=500)


class AdvanceCreate(RequestModel):
    """The fees billed in advance on a new draft: one manual line marked as an advance."""

    description: str = Field(min_length=1, max_length=1000, examples=["Fees in advance 2026/27"])
    net_amount: PositiveMoney = Field(description="The advance before VAT")
    vat_rate_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None


class AdvanceLineCreate(RequestModel):
    """One line of fees billed in advance: quantity x unit amount before VAT, more than zero."""

    description: str = Field(min_length=1, max_length=1000, examples=["Accounts 2026/27"])
    quantity: Quantity = Decimal(1)
    unit_amount: PositiveMoney = Field(description="The price of one unit before VAT")
    vat_rate_id: uuid.UUID | None = None


class InvoiceCreate(WipSelection):
    client_id: uuid.UUID
    issue_date: date | None = None
    due_date: date | None = None
    reference: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)
    valuation_basis: Literal["contract", "job", "team"] | None = Field(
        default=None,
        description=(
            "What the lines were valued on when the draft was prepared: the `contract` value, the "
            "`job` value or the `team` value (see `GET /clients/{id}/billing-summary`). Kept in "
            "the audit entry `invoice.draft_created`; the amounts are the lines' own"
        ),
    )
    advance: AdvanceCreate | None = Field(
        default=None,
        description=(
            "Fees billed in advance: adds one manual line marked `is_advance`, to which the "
            "client's time is assigned once the invoice is issued "
            "(`POST /invoices/{id}/lines/{line_id}/assign`)"
        ),
    )
    advance_lines: list[AdvanceLineCreate] | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Fees billed in advance on several lines: each adds a manual line marked "
            "`is_advance` (net = round(quantity x unit_amount)). Not together with `advance`"
        ),
    )

    _blank = field_validator("reference", "notes")(_blank_is_null)

    @model_validator(mode="after")
    def _one_advance_shape(self) -> Self:
        if self.advance is not None and self.advance_lines is not None:
            raise ValueError("advance_lines: Give either advance or advance_lines, not both")
        return self


class InvoiceAddWip(WipSelection):
    @model_validator(mode="after")
    def _something(self) -> Self:
        if not self.time_entry_ids and not self.disbursement_ids:
            raise ValueError("time_entry_ids: Choose at least one WIP item")
        return self


class InvoiceBulkAction(RequestModel):
    invoice_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_BULK_IDS)
    action: Literal["delete"] = Field(
        description="`delete`: delete draft and void invoices (the only bulk action: issued "
        "invoices are locked until they are voided)"
    )
    value: None = Field(default=None, description="No action takes a value yet")


class InvoiceUpdate(RequestModel):
    issue_date: date | None = None
    due_date: date | None = None
    reference: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)

    _blank = field_validator("reference", "notes")(_blank_is_null)


class LineCreate(RequestModel):
    description: str = Field(min_length=1, max_length=1000)
    quantity: Quantity = Decimal(1)
    unit_amount: SignedMoney
    net_amount: SignedMoney | None = Field(
        default=None, description="Overrides round(quantity x unit_amount)"
    )
    vat_rate_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    position: int | None = Field(default=None, ge=1, le=10000)
    is_advance: bool = Field(
        default=False,
        description=(
            "Fees billed in advance: once the invoice is issued, time is assigned to the line "
            "later. Its net must be more than zero"
        ),
    )


class AdvanceAssign(RequestModel):
    """Items of the invoice's client to assign to, or take off, an advance line."""

    time_entry_ids: ItemIds = Field(default_factory=list)
    disbursement_ids: ItemIds = Field(default_factory=list)

    @model_validator(mode="after")
    def _something(self) -> Self:
        if not self.time_entry_ids and not self.disbursement_ids:
            raise ValueError("time_entry_ids: Choose at least one item")
        return self


class LineUpdate(RequestModel):
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    quantity: Quantity | None = None
    unit_amount: SignedMoney | None = None
    net_amount: SignedMoney | None = None
    vat_rate_id: uuid.UUID | None = Field(
        default=None, description="null re-applies the default rate resolution"
    )
    position: int | None = Field(default=None, ge=1, le=10000)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        _reject_nulls(self, "description", "quantity", "unit_amount", "net_amount", "position")
        return self


class InvoiceIssue(RequestModel):
    issue_date: date | None = Field(default=None, description="Default: the draft's, else today")
    due_date: date | None = Field(default=None, description="Default: issue date + terms")


class InvoiceVoid(RequestModel):
    reason: str = Field(min_length=1, max_length=500)
    notify_client: bool = Field(
        default=False,
        description=(
            "Email the client that the invoice is cancelled (the Invoice cancelled template), "
            "to the addresses an invoice email goes to. Needs `invoice.send`"
        ),
    )


class InvoiceSubmit(RequestModel):
    note: str | None = Field(
        default=None, max_length=500, description="Optional message for the approvers"
    )

    _blank = field_validator("note")(_blank_is_null)


class InvoiceApprove(RequestModel):
    note: str | None = Field(default=None, max_length=500, description="Optional comment")

    _blank = field_validator("note")(_blank_is_null)


class InvoiceReject(RequestModel):
    reason: str = Field(
        min_length=1, max_length=500, description="Why it is rejected (shown to the preparer)"
    )


class InvoiceSend(RequestModel):
    to: list[EmailStr] | None = Field(
        default=None,
        min_length=1,
        max_length=10,
        description=(
            "Default: the client's invoice email, else its billing contacts, else its "
            "primary contact, else the client's own email"
        ),
    )


class CreditNoteLineCreate(RequestModel):
    description: str = Field(min_length=1, max_length=1000)
    net_amount: PositiveMoney
    vat_rate_id: uuid.UUID | None = None


class CreditNoteCreate(RequestModel):
    lines: list[CreditNoteLineCreate] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)
    issue_date: date | None = Field(default=None, description="Default: today")


# --- Responses --------------------------------------------------------------------------


class LineWipRead(BaseModel):
    """The WIP a line bills: the sum of its items' WIP values against what they billed."""

    item_count: int
    time_entry_ids: list[uuid.UUID]
    disbursement_ids: list[uuid.UUID]
    wip_value: int
    billed_value: int
    # billed - wip: positive is a write-up, negative a write-off
    adjustment: int


class AdvanceItemRead(BaseModel):
    """A piece of work assigned to an advance line."""

    kind: Literal["time", "disbursement"]
    id: uuid.UUID
    date: DateType
    person: PersonRef | None = Field(description="Who did the work (time only)")
    description: str | None
    minutes: int | None
    hours: Decimal | None
    wip_value: int = Field(description="What the work was worth")
    billed_value: int = Field(description="What the advance covers of it; the rest is written off")
    assigned_on: DateType | None


class AdvanceRead(BaseModel):
    """How much of an advance line the assigned work has used."""

    amount: int = Field(description="The advance: the line's net")
    used: int = Field(description="The billed value of the work assigned to it")
    remaining: int = Field(description="What is left of the advance to assign work to")
    wip_value: int = Field(description="What the assigned work was worth")
    written_off: int = Field(description="Work assigned beyond the advance: written off")
    items: list[AdvanceItemRead]


class LineRead(BaseModel):
    id: uuid.UUID
    position: int
    kind: str
    description: str
    job_id: uuid.UUID | None
    service_id: uuid.UUID | None
    quantity: Decimal
    unit_amount: int
    net_amount: int
    vat_rate_id: uuid.UUID | None
    vat_rate_bp: int
    vat_amount: int
    gross_amount: int
    wip: LineWipRead | None
    is_advance: bool = False
    advance: AdvanceRead | None = Field(
        default=None, description="On an advance line: the work assigned to it and what is left"
    )

    @classmethod
    def from_model(
        cls, line: InvoiceLine, wip: LineWipRead | None, advance: AdvanceRead | None = None
    ) -> "LineRead":
        return cls(
            id=line.id,
            position=line.position,
            kind=line.kind,
            description=line.description,
            job_id=line.job_id,
            service_id=line.service_id,
            quantity=line.quantity,
            unit_amount=line.unit_amount,
            net_amount=line.net_amount,
            vat_rate_id=line.vat_rate_id,
            vat_rate_bp=line.vat_rate_bp,
            vat_amount=line.vat_amount,
            gross_amount=line.net_amount + line.vat_amount,
            wip=wip,
            is_advance=line.is_advance,
            advance=advance,
        )


class VatSummaryRow(BaseModel):
    vat_rate_bp: int
    net: int
    vat: int


class InvoicePaymentRead(BaseModel):
    allocation_id: uuid.UUID
    payment_id: uuid.UUID
    received_on: date
    method: str
    reference: str | None
    amount: int


class CreditNoteSummary(BaseModel):
    id: uuid.UUID
    number: str
    issue_date: date
    reason: str
    gross_total: int


class InvoiceSummaryRead(BaseModel):
    id: uuid.UUID
    number: str | None
    status: str
    display_status: DisplayStatus
    client: ClientRef | None
    issue_date: date | None
    due_date: date | None
    reference: str | None
    currency: str
    net_total: int
    vat_total: int
    gross_total: int
    paid_total: int
    credited_total: int
    balance: int
    approval_required: bool = Field(
        description=(
            "The invoice needs approval before it can be issued under the practice's current "
            "settings (false once issued or void)"
        )
    )
    sent_at: datetime | None
    created_at: datetime

    @classmethod
    def build(
        cls,
        invoice: Invoice,
        client: Client | None,
        display_status: DisplayStatus,
        approval_required: bool = False,
    ) -> "InvoiceSummaryRead":
        return cls(
            id=invoice.id,
            number=invoice.number,
            status=invoice.status,
            display_status=display_status,
            client=ClientRef.from_model(client) if client else None,
            issue_date=invoice.issue_date,
            due_date=invoice.due_date,
            reference=invoice.reference,
            currency=invoice.currency,
            net_total=invoice.net_total,
            vat_total=invoice.vat_total,
            gross_total=invoice.gross_total,
            paid_total=invoice.paid_total,
            credited_total=invoice.credited_total,
            balance=invoice.balance,
            approval_required=approval_required,
            sent_at=invoice.sent_at,
            created_at=invoice.created_at,
        )


class InvoiceWipTotals(BaseModel):
    wip_value: int
    billed_value: int
    adjustment: int


class ApprovalEventRead(BaseModel):
    """One step of the invoice's approval history."""

    id: uuid.UUID
    action: ApprovalAction
    actor: PersonRef
    note: str | None
    gross_total: int = Field(description="The invoice total when the step happened (pence)")
    created_at: datetime


class InvoiceRead(InvoiceSummaryRead):
    notes: str | None
    issued_at: datetime | None
    issued_by: uuid.UUID | None
    voided_at: datetime | None
    void_reason: str | None
    deleted_at: datetime | None = Field(
        description="Set when the void invoice was deleted: it is left out of every list"
    )
    email_recipients: list[str] = Field(
        description=(
            "Where the invoice's emails go by default: the client's invoice email, else its "
            "billing contacts, else its primary contact, else the client's email (empty: nowhere)"
        )
    )
    created_by: uuid.UUID | None
    approvals: list[ApprovalEventRead]
    can_approve: bool = Field(
        description=(
            "The caller may approve it now: holds `invoice.approve`, the invoice is awaiting "
            "approval and the caller is not its preparer or submitter (an Owner may be)"
        )
    )
    lines: list[LineRead]
    vat_summary: list[VatSummaryRow]
    wip: InvoiceWipTotals
    payments: list[InvoicePaymentRead]
    credit_notes: list[CreditNoteSummary]


class InvoiceStatusCounts(BaseModel):
    """Invoices per display status; each invoice is counted once."""

    draft: int
    awaiting_approval: int
    approved: int
    issued: int
    part_paid: int
    paid: int
    credited: int
    overdue: int
    void: int
    total: int


class InvoiceListSummary(BaseModel):
    counts: InvoiceStatusCounts
    outstanding: int = Field(description="Sum of the balances of issued invoices (pence)")
    overdue: int = Field(description="Sum of the balances of overdue invoices (pence)")
    awaiting_approval_gross: int = Field(description="Gross total of invoices awaiting approval")


class InvoiceSent(BaseModel):
    invoice_id: uuid.UUID
    to: list[str]
    queued: bool


class CreditNoteLineRead(BaseModel):
    id: uuid.UUID
    position: int
    description: str
    net_amount: int
    vat_rate_id: uuid.UUID | None
    vat_rate_bp: int
    vat_amount: int
    gross_amount: int

    @classmethod
    def from_model(cls, line: CreditNoteLine) -> "CreditNoteLineRead":
        return cls(
            id=line.id,
            position=line.position,
            description=line.description,
            net_amount=line.net_amount,
            vat_rate_id=line.vat_rate_id,
            vat_rate_bp=line.vat_rate_bp,
            vat_amount=line.vat_amount,
            gross_amount=line.net_amount + line.vat_amount,
        )


class CreditNoteRead(BaseModel):
    id: uuid.UUID
    number: str
    invoice_id: uuid.UUID
    invoice_number: str | None
    client: ClientRef | None
    issue_date: date
    reason: str
    net_total: int
    vat_total: int
    gross_total: int
    lines: list[CreditNoteLineRead] | None
    created_by: uuid.UUID | None
    created_at: datetime

    @classmethod
    def build(
        cls,
        note: CreditNote,
        *,
        client: Client | None,
        invoice_number: str | None,
        lines: list[CreditNoteLine] | None,
    ) -> "CreditNoteRead":
        return cls(
            id=note.id,
            number=note.number,
            invoice_id=note.invoice_id,
            invoice_number=invoice_number,
            client=ClientRef.from_model(client) if client else None,
            issue_date=note.issue_date,
            reason=note.reason,
            net_total=note.net_total,
            vat_total=note.vat_total,
            gross_total=note.gross_total,
            lines=[CreditNoteLineRead.from_model(line) for line in lines]
            if lines is not None
            else None,
            created_by=note.created_by,
            created_at=note.created_at,
        )

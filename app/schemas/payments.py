"""Request and response models of payments, allocations and the client statement."""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.models import Client, Invoice, Payment, PaymentAllocation
from app.schemas.billing import ClientRef
from app.schemas.common import RequestModel

PositiveMoney = Annotated[int, Field(gt=0, le=10**12, description="Minor units (pence)")]
PaymentMethod = Literal["bank_transfer", "card", "direct_debit", "cash", "cheque", "other"]
PaymentState = Literal["unallocated", "part_allocated", "allocated", "void", "reversed"]


def _blank_is_null(value: str | None) -> str | None:
    return value or None


class AllocationIn(RequestModel):
    invoice_id: uuid.UUID
    amount: PositiveMoney


def _unique_invoices(allocations: list[AllocationIn] | None) -> None:
    if allocations is not None:
        ids = [a.invoice_id for a in allocations]
        if len(set(ids)) != len(ids):
            raise ValueError("allocations: Each invoice may appear only once")


class PaymentCreate(RequestModel):
    client_id: uuid.UUID
    received_on: date
    amount: PositiveMoney
    method: PaymentMethod = "bank_transfer"
    reference: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=1000)
    allocations: list[AllocationIn] | None = Field(default=None, max_length=200)
    auto_allocate: bool = Field(
        default=False, description="Allocate to the client's oldest open invoices first"
    )

    _blank = field_validator("reference", "notes")(_blank_is_null)

    @model_validator(mode="after")
    def _one_way(self) -> Self:
        if self.allocations and self.auto_allocate:
            raise ValueError("allocations: Give allocations or auto_allocate, not both")
        _unique_invoices(self.allocations)
        return self


class AllocationsCreate(RequestModel):
    allocations: list[AllocationIn] | None = Field(default=None, min_length=1, max_length=200)
    auto_allocate: bool = False

    @model_validator(mode="after")
    def _one_way(self) -> Self:
        if bool(self.allocations) == self.auto_allocate:
            raise ValueError("allocations: Give either allocations or auto_allocate")
        _unique_invoices(self.allocations)
        return self


class PaymentVoid(RequestModel):
    reason: str | None = Field(default=None, max_length=500)


class PaymentReverse(RequestModel):
    reason: str = Field(
        min_length=1, max_length=500, description="Why the payment is reversed (required)"
    )
    bounced: bool = Field(
        default=False, description="The money never arrived: a bounced cheque or returned debit"
    )


class AllocationRead(BaseModel):
    id: uuid.UUID
    invoice_id: uuid.UUID
    invoice_number: str | None
    amount: int
    created_at: datetime

    @classmethod
    def build(cls, allocation: PaymentAllocation, invoice: Invoice) -> "AllocationRead":
        return cls(
            id=allocation.id,
            invoice_id=allocation.invoice_id,
            invoice_number=invoice.number,
            amount=allocation.amount,
            created_at=allocation.created_at,
        )


def payment_state(payment: Payment) -> PaymentState:
    if payment.reversed_at is not None:
        return "reversed"
    if payment.voided_at is not None:
        return "void"
    if payment.allocated_total == 0:
        return "unallocated"
    if payment.allocated_total < payment.amount:
        return "part_allocated"
    return "allocated"


class PaymentRead(BaseModel):
    id: uuid.UUID
    client: ClientRef | None
    received_on: date
    amount: int
    method: str
    reference: str | None
    notes: str | None
    allocated_total: int
    unallocated: int
    state: PaymentState
    voided_at: datetime | None = Field(
        description="When it was voided; also set for a reversed payment (which is void too)"
    )
    reversed_at: datetime | None = Field(description="When it was reversed (bounced), if it was")
    reversed_by: uuid.UUID | None
    reversal_reason: str | None
    bounced: bool = Field(description="A reversed payment whose money never arrived")
    created_by: uuid.UUID | None
    created_at: datetime
    allocations: list[AllocationRead] | None

    @classmethod
    def build(
        cls,
        payment: Payment,
        client: Client | None,
        allocations: list[tuple[PaymentAllocation, Invoice]] | None,
    ) -> "PaymentRead":
        return cls(
            id=payment.id,
            client=ClientRef.from_model(client) if client else None,
            received_on=payment.received_on,
            amount=payment.amount,
            method=payment.method,
            reference=payment.reference,
            notes=payment.notes,
            allocated_total=payment.allocated_total,
            unallocated=payment.unallocated,
            state=payment_state(payment),
            voided_at=payment.voided_at,
            reversed_at=payment.reversed_at,
            reversed_by=payment.reversed_by,
            reversal_reason=payment.reversal_reason,
            bounced=payment.bounced,
            created_by=payment.created_by,
            created_at=payment.created_at,
            allocations=[AllocationRead.build(a, i) for a, i in allocations]
            if allocations is not None
            else None,
        )


# --- Statement --------------------------------------------------------------------------


DateType = date


class StatementEntry(BaseModel):
    date: DateType
    type: Literal["invoice", "credit_note", "payment", "payment_reversal"]
    id: uuid.UUID
    number: str | None
    description: str
    # What the entry adds to the balance: invoices and payment reversals +, credit notes and
    # payments -.
    amount: int
    balance: int


class StatementRead(BaseModel):
    client: ClientRef
    currency: str
    date_from: date | None
    date_to: date
    opening_balance: int
    entries: list[StatementEntry]
    invoiced: int
    credited: int
    paid: int
    closing_balance: int
    # Cash received up to date_to that is not allocated to any invoice (on account).
    unallocated_cash: int

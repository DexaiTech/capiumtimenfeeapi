"""Request and response models of the WIP views, the ledger, adjustments, transfers and the WIP
of credit notes. Money is integer pence, dates are ISO days in the practice time zone."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.schemas.billing import ClientRef, PersonRef, PositiveMoney
from app.schemas.common import RequestModel

WipComponent = Literal["time", "fixed_fee", "expense"]
WipItemKind = Literal["time", "fixed_fee", "expense", "disbursement", "adjustment"]
WipGroupBy = Literal["client", "job", "partner", "manager", "service"]
AdjustmentKind = Literal["write_on", "write_off"]
AdjustmentStatus = Literal["pending", "approved", "rejected", "reversed"]
MovementKind = Literal[
    "write_on",
    "write_off",
    "transfer_out",
    "transfer_in",
    "credit_return",
    "credit_write_off",
    "reversal",
]
MovementScope = Literal["amount", "items"]
CreditWipMode = Literal["return", "write_off"]
TransferStatus = Literal["applied", "reversed"]

_REASON = Field(min_length=1, max_length=500, description="Why (required)")


class WipJobRef(BaseModel):
    id: uuid.UUID
    number: str | None
    name: str | None
    billing_basis: str | None = None


class WipAges(BaseModel):
    """WIP value by age of the item date as at the day: 0-30, 31-60, 61-90 and 90+ days."""

    days_0_30: int
    days_31_60: int
    days_61_90: int
    days_90_plus: int


class WipGroupTotals(BaseModel):
    minutes: int
    hours: Decimal
    time: int = Field(description="Time WIP")
    fixed_fee: int = Field(description="Fixed fee WIP")
    expense: int = Field(description="Expense WIP: disbursements and approved expense lines")
    total: int
    ages: WipAges
    oldest_date: date | None
    item_count: int
    over_90_bp: int | None = Field(description="Share of the total older than 90 days, basis points")


class WipGroupRow(WipGroupTotals):
    group_by: WipGroupBy
    key: uuid.UUID | None = Field(
        description="Id of the client, job, partner, manager or service; null for 'no job', "
        "'unassigned' or 'no service'"
    )
    name: str
    code: str | None = Field(description="Client code or job number")
    client: ClientRef | None = Field(description="The job's client (group_by=job)")


class WipSummaryRead(BaseModel):
    group_by: WipGroupBy
    as_at: date
    rows: list[WipGroupRow]
    totals: WipGroupTotals


# --- Adjustments ------------------------------------------------------------------------


class AdjustmentCreate(RequestModel):
    client_id: uuid.UUID
    job_id: uuid.UUID | None = Field(
        default=None, description="The job the amount applies to; null for the client's WIP"
    )
    kind: AdjustmentKind = Field(description="`write_on` adds WIP, `write_off` removes it")
    amount: PositiveMoney
    reason: str = _REASON
    component: WipComponent | None = Field(
        default=None,
        description="Which WIP it adjusts. Default: `fixed_fee` on a fixed-fee job, else `time`",
    )
    effective_date: date | None = Field(
        default=None, description="Default: today in the practice's time zone; never in the future"
    )


class AdjustmentApprove(RequestModel):
    note: str | None = Field(default=None, max_length=500, description="Optional note")


class AdjustmentReject(RequestModel):
    reason: str = _REASON


class AdjustmentReverse(RequestModel):
    reason: str = _REASON


class AdjustmentRead(BaseModel):
    id: uuid.UUID
    client: ClientRef | None
    job: WipJobRef | None
    kind: AdjustmentKind
    component: WipComponent
    amount: int = Field(description="Always positive; `kind` says whether it adds or removes WIP")
    signed_amount: int = Field(description="`amount` for a write-on, `-amount` for a write-off")
    effective_date: date
    reason: str
    status: AdjustmentStatus
    approval_required: bool = Field(
        description="The amount was above the practice's threshold when it was requested"
    )
    threshold: int = Field(description="The threshold that applied (pence)")
    requested_by: PersonRef
    requested_at: datetime
    decided_by: PersonRef | None
    decided_at: datetime | None
    decision_note: str | None
    reversed_by: PersonRef | None
    reversed_at: datetime | None
    reversal_reason: str | None
    in_force: bool = Field(
        description="Approved and not reversed or settled: the amount is part of the job's WIP"
    )
    movement_id: uuid.UUID | None = Field(description="The ledger row, once the adjustment is in effect")


class AdjustmentCounts(BaseModel):
    pending: int
    approved: int
    rejected: int
    reversed: int
    pending_value: int = Field(description="Total amount of the pending adjustments")


# --- Transfers --------------------------------------------------------------------------


class TransferItemRef(RequestModel):
    kind: Literal["time", "disbursement", "expense"]
    id: uuid.UUID


class TransferCreate(RequestModel):
    from_job_id: uuid.UUID
    to_job_id: uuid.UUID
    items: list[TransferItemRef] | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Move the value of these unbilled items of the source job",
    )
    amount: PositiveMoney | None = Field(default=None, description="Or move this amount")
    reason: str = _REASON
    component: WipComponent | None = Field(
        default=None,
        description="With `amount`: which WIP moves. Default: `fixed_fee` on a fixed-fee job, else `time`",
    )
    effective_date: date | None = Field(
        default=None, description="Default: today in the practice's time zone; never in the future"
    )

    @model_validator(mode="after")
    def _items_or_amount(self) -> Self:
        if (self.items is None) == (self.amount is None):
            raise ValueError("items: Give either items or amount")
        return self


class TransferReverse(RequestModel):
    reason: str = _REASON


class TransferItemRead(BaseModel):
    kind: Literal["time", "disbursement", "expense"]
    id: uuid.UUID
    value: int


class TransferRead(BaseModel):
    id: uuid.UUID
    client: ClientRef | None
    from_job: WipJobRef | None
    to_job: WipJobRef | None
    component: WipComponent
    amount: int
    effective_date: date
    reason: str
    items: list[TransferItemRead]
    status: TransferStatus
    created_by: PersonRef
    created_at: datetime
    reversed_by: PersonRef | None
    reversed_at: datetime | None
    reversal_reason: str | None


# --- Ledger -----------------------------------------------------------------------------


class MovementRead(BaseModel):
    id: uuid.UUID
    kind: MovementKind
    scope: MovementScope = Field(
        description="`amount`: adjusts the WIP of the job. `items`: a memo of a change to items"
    )
    component: WipComponent
    client: ClientRef | None
    job: WipJobRef | None
    amount: int = Field(description="Signed: positive adds WIP value, negative removes it")
    effective_date: date
    reason: str
    actor: PersonRef | None
    approval_state: Literal["not_required", "approved"]
    approved_by: PersonRef | None
    approved_at: datetime | None
    reversal_of_id: uuid.UUID | None
    reversed: bool = Field(description="A later `reversal` row cancels this movement")
    source_type: Literal["adjustment", "transfer", "write_off", "credit_note"] | None
    source_id: uuid.UUID | None
    item_count: int
    created_at: datetime


# --- Credit notes -----------------------------------------------------------------------


class CreditNoteWipCreate(RequestModel):
    mode: CreditWipMode = Field(
        description="`return`: the credited items go back to WIP. `write_off`: write the credit off"
    )
    reason: str = _REASON


class CreditNoteWipItem(BaseModel):
    kind: Literal["time", "disbursement", "expense"]
    id: uuid.UUID
    wip_value: int


class CreditNoteWipRead(BaseModel):
    id: uuid.UUID
    credit_note_id: uuid.UUID
    invoice_id: uuid.UUID
    client: ClientRef | None
    mode: CreditWipMode
    reason: str
    net_credited: int
    wip_value: int = Field(description="WIP value that came back (return) or was written off")
    item_count: int
    items: list[CreditNoteWipItem]
    created_by: PersonRef
    created_at: datetime

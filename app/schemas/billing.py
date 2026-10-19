"""Request and response models of disbursements and work in progress (WIP)."""

import uuid
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.models import Client, Disbursement, Job, WipWriteOff
from app.schemas.common import RequestModel

Money = Annotated[int, Field(ge=0, le=10**12, description="Minor units (pence)")]
PositiveMoney = Annotated[int, Field(gt=0, le=10**12, description="Minor units (pence)")]
BillingStatus = Literal["unbilled", "in_draft", "billed", "written_off"]


def hours_of(minutes: int) -> Decimal:
    """Minutes as hours to two decimal places, rounded half up."""
    return (Decimal(minutes) / 60).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _reject_nulls(model: BaseModel, *fields: str) -> None:
    for field in fields:
        if field in model.model_fields_set and getattr(model, field) is None:
            raise ValueError(f"{field}: This field cannot be null")


def _blank_is_null(value: str | None) -> str | None:
    return value or None


class ClientRef(BaseModel):
    id: uuid.UUID
    code: str
    name: str

    @classmethod
    def from_model(cls, client: Client) -> "ClientRef":
        return cls(id=client.id, code=client.code, name=client.name)


class JobRef(BaseModel):
    id: uuid.UUID
    number: str
    name: str
    billing_basis: str

    @classmethod
    def from_model(cls, job: Job) -> "JobRef":
        return cls(id=job.id, number=job.number, name=job.name, billing_basis=job.billing_basis)


class PersonRef(BaseModel):
    user_id: uuid.UUID
    full_name: str | None


# --- Disbursements ----------------------------------------------------------------------


class DisbursementCreate(RequestModel):
    client_id: uuid.UUID
    job_id: uuid.UUID | None = None
    incurred_on: date
    description: str = Field(min_length=1, max_length=500)
    supplier: str | None = Field(default=None, max_length=200)
    amount: PositiveMoney
    vat_rate_id: uuid.UUID | None = Field(
        default=None, description="VAT charged on the recharge; null uses the default rate"
    )
    is_billable: bool = True

    _supplier = field_validator("supplier")(_blank_is_null)


class DisbursementUpdate(RequestModel):
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    incurred_on: date | None = None
    description: str | None = Field(default=None, min_length=1, max_length=500)
    supplier: str | None = Field(default=None, max_length=200)
    amount: PositiveMoney | None = None
    vat_rate_id: uuid.UUID | None = None
    is_billable: bool | None = None

    _supplier = field_validator("supplier")(_blank_is_null)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        _reject_nulls(self, "client_id", "incurred_on", "description", "amount", "is_billable")
        return self


class DisbursementRead(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    job_id: uuid.UUID | None
    incurred_on: date
    description: str
    supplier: str | None
    amount: int
    vat_rate_id: uuid.UUID | None
    is_billable: bool
    billing_status: str
    created_by: uuid.UUID | None
    created_at: datetime

    @classmethod
    def from_model(cls, row: Disbursement) -> "DisbursementRead":
        return cls(
            id=row.id,
            client_id=row.client_id,
            job_id=row.job_id,
            incurred_on=row.incurred_on,
            description=row.description,
            supplier=row.supplier,
            amount=row.amount,
            vat_rate_id=row.vat_rate_id,
            is_billable=row.is_billable,
            billing_status=row.billing_status,
            created_by=row.created_by,
            created_at=row.created_at,
        )


# --- WIP --------------------------------------------------------------------------------


class AgeBuckets(BaseModel):
    """WIP value by age of the item date: 0-30, 31-60, 61-90 and 91+ days."""

    days_0_30: int
    days_31_60: int
    days_61_90: int
    days_91_plus: int


class WipComposition(BaseModel):
    """Total WIP = Time WIP + Fixed fee WIP + Expense WIP (disbursements count as expense)."""

    time: int
    fixed_fee: int
    expense: int
    total: int


class FixedFeeRead(BaseModel):
    """What a fixed-fee WIP item is made of (docs/modules/wip.md, "Fixed fees")."""

    fee: int = Field(description="The job's fixed fee")
    recognised: int = Field(description="The part of the fee recognised as WIP so far")
    billed: int = Field(description="Net already on live fixed-fee invoice lines of the job")
    mode: str = Field(description="`on_completion` or `by_budget_progress`")
    progress_bp: int | None = Field(description="Budget hours used, basis points (progress mode)")
    time_entry_ids: list[uuid.UUID] = Field(
        description="The job's unbilled time entries: select these to bill the fee"
    )


class WipClientSummary(BaseModel):
    client: ClientRef
    partner_user_id: uuid.UUID | None
    manager_user_id: uuid.UUID | None
    minutes: int
    hours: Decimal
    time_value: int = Field(description="Charge value of all WIP time, fixed-fee jobs included")
    disbursement_value: int = Field(description="Disbursements only")
    total: int = Field(description="Total WIP: `composition.total`")
    oldest_date: date | None
    ages: AgeBuckets
    item_count: int
    expense_value: int = Field(default=0, description="Approved expense lines only")
    composition: WipComposition | None = Field(
        default=None, description="Time, fixed fee and expense WIP; their sum is `total`"
    )


DateType = date


class WipItemRead(BaseModel):
    kind: Literal["time", "disbursement", "expense", "fixed_fee", "adjustment"]
    id: uuid.UUID
    date: DateType
    job: JobRef | None
    person: PersonRef | None
    description: str | None
    minutes: int | None
    hours: Decimal | None
    rate: int | None
    value: int = Field(description="What counts in the totals (pence)")
    activity_code_id: uuid.UUID | None = None
    supplier: str | None = None
    client: ClientRef | None = None
    age_days: int | None = Field(default=None, description="Days since the item date")
    component: Literal["time", "fixed_fee", "expense"] | None = Field(
        default=None, description="Which part of Total WIP the item counts in"
    )
    charge_value: int | None = Field(
        default=None, description="Time only: its value at the charge-out rate"
    )
    selectable: bool | None = Field(
        default=None,
        description="Billing may take this item by its id (time, disbursement, expense)",
    )
    claim_id: uuid.UUID | None = Field(default=None, description="Expense lines: the claim")
    fixed_fee: FixedFeeRead | None = None
    movement_kind: str | None = Field(
        default=None, description="Adjustments: the ledger kind (write_on, transfer_in, ...)"
    )


class WipTotals(BaseModel):
    minutes: int
    hours: Decimal
    time_value: int
    disbursement_value: int
    total: int


class ClientWipRead(BaseModel):
    client: ClientRef
    items: list[WipItemRead]
    totals: WipTotals
    composition: WipComposition | None = None


class WipItemRef(RequestModel):
    time_entry_id: uuid.UUID | None = None
    disbursement_id: uuid.UUID | None = None
    expense_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        given = [v for v in (self.time_entry_id, self.disbursement_id, self.expense_id) if v]
        if len(given) != 1:
            raise ValueError("Give exactly one of time_entry_id, disbursement_id and expense_id")
        return self


class WriteOffCreate(RequestModel):
    client_id: uuid.UUID
    items: list[WipItemRef] | None = Field(default=None, min_length=1, max_length=1000)
    job_id: uuid.UUID | None = Field(default=None, description="Write off all WIP of this job")
    reason: str = Field(min_length=1, max_length=500)
    written_off_on: date | None = Field(default=None, description="Default: today")

    @model_validator(mode="after")
    def _items_or_job(self) -> Self:
        if (self.items is None) == (self.job_id is None):
            raise ValueError("items: Give either items or job_id")
        return self


class WriteOffItemRead(BaseModel):
    kind: Literal["time", "disbursement", "expense"]
    id: uuid.UUID
    wip_value: int


class WriteOffRead(BaseModel):
    id: uuid.UUID
    client: ClientRef | None
    job_id: uuid.UUID | None
    written_off_on: date
    reason: str
    total_wip_value: int
    item_count: int
    items: list[WriteOffItemRead] | None = None
    created_by: uuid.UUID | None
    created_at: datetime

    @classmethod
    def from_model(
        cls,
        row: WipWriteOff,
        *,
        client: Client | None,
        item_count: int,
        items: list[WriteOffItemRead] | None = None,
    ) -> "WriteOffRead":
        return cls(
            id=row.id,
            client=ClientRef.from_model(client) if client else None,
            job_id=row.job_id,
            written_off_on=row.written_off_on,
            reason=row.reason,
            total_wip_value=row.total_wip_value,
            item_count=item_count,
            items=items,
            created_by=row.created_by,
            created_at=row.created_at,
        )

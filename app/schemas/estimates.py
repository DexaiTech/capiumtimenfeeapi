"""Request and response models of estimates."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.models import Client, Estimate, EstimateLine
from app.schemas.billing import ClientRef
from app.schemas.common import RequestModel

SignedMoney = Annotated[
    int, Field(ge=-(10**12), le=10**12, description="Minor units (pence); negative = discount")
]
Quantity = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
EstimateStatus = Literal["draft", "sent", "accepted", "declined"]
# ``expired``: sent, and past its expiry date. Derived, never stored.
EstimateDisplayStatus = Literal["draft", "sent", "accepted", "declined", "expired"]


def _blank_is_null(value: str | None) -> str | None:
    return value or None


class LineInput(RequestModel):
    """One line of an estimate or a recurring template. net = round(quantity x unit_amount)."""

    description: str = Field(min_length=1, max_length=1000)
    quantity: Quantity = Decimal(1)
    unit_amount: SignedMoney
    vat_rate_id: uuid.UUID | None = Field(
        default=None, description="Default: the service's, the client's, else the practice's"
    )
    service_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None


class EstimateCreate(RequestModel):
    client_id: uuid.UUID
    job_id: uuid.UUID | None = None
    expiry_date: date | None = None
    reference: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[LineInput] = Field(min_length=1, max_length=200)

    _blank = field_validator("reference", "notes")(_blank_is_null)


class EstimateUpdate(RequestModel):
    """Only a draft can change. ``lines``, when sent, replaces every line."""

    job_id: uuid.UUID | None = None
    expiry_date: date | None = None
    reference: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[LineInput] | None = Field(default=None, min_length=1, max_length=200)

    _blank = field_validator("reference", "notes")(_blank_is_null)

    @model_validator(mode="after")
    def _no_null_lines(self) -> Self:
        if "lines" in self.model_fields_set and self.lines is None:
            raise ValueError("lines: Cannot be null")
        return self


class EstimateSend(RequestModel):
    issue_date: date | None = Field(default=None, description="Default: today")
    expiry_date: date | None = Field(default=None, description="Default: the draft's own")


class EstimateLineRead(BaseModel):
    id: uuid.UUID
    position: int
    description: str
    job_id: uuid.UUID | None
    service_id: uuid.UUID | None
    quantity: Decimal
    unit_amount: int
    net_amount: int
    vat_rate_id: uuid.UUID | None
    vat_rate_bp: int
    vat_amount: int

    @classmethod
    def from_model(cls, line: EstimateLine) -> Self:
        return cls(
            id=line.id,
            position=line.position,
            description=line.description,
            job_id=line.job_id,
            service_id=line.service_id,
            quantity=line.quantity,
            unit_amount=line.unit_amount,
            net_amount=line.net_amount,
            vat_rate_id=line.vat_rate_id,
            vat_rate_bp=line.vat_rate_bp,
            vat_amount=line.vat_amount,
        )


class EstimateSummaryRead(BaseModel):
    id: uuid.UUID
    number: str | None
    status: EstimateStatus
    display_status: EstimateDisplayStatus
    client: ClientRef | None
    job_id: uuid.UUID | None
    issue_date: date | None
    expiry_date: date | None
    reference: str | None
    currency: str
    net_total: int
    vat_total: int
    gross_total: int
    sent_at: datetime | None
    responded_at: datetime | None
    converted_invoice_id: uuid.UUID | None
    created_at: datetime

    @classmethod
    def build(
        cls, estimate: Estimate, client: Client | None, display_status: EstimateDisplayStatus
    ) -> Self:
        return cls(
            id=estimate.id,
            number=estimate.number,
            status=estimate.status,
            display_status=display_status,
            client=ClientRef.from_model(client) if client else None,
            job_id=estimate.job_id,
            issue_date=estimate.issue_date,
            expiry_date=estimate.expiry_date,
            reference=estimate.reference,
            currency=estimate.currency,
            net_total=estimate.net_total,
            vat_total=estimate.vat_total,
            gross_total=estimate.gross_total,
            sent_at=estimate.sent_at,
            responded_at=estimate.responded_at,
            converted_invoice_id=estimate.converted_invoice_id,
            created_at=estimate.created_at,
        )


class EstimateRead(EstimateSummaryRead):
    notes: str | None
    lines: list[EstimateLineRead]


class EstimateStatusCounts(BaseModel):
    draft: int
    sent: int
    accepted: int
    declined: int
    expired: int
    total: int


class EstimateListSummary(BaseModel):
    counts: EstimateStatusCounts
    open_gross: int = Field(description="Gross value of the estimates sent and still open")


class EstimateConverted(BaseModel):
    estimate: EstimateRead
    invoice_id: uuid.UUID

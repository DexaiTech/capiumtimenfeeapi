"""Request and response models of practice settings: VAT rates, services, activity codes
and number sequences."""

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.models import ActivityCode, DocumentSequence, Service, VatRate
from app.schemas.common import RequestModel
from app.services.sequences import format_number

Money = Annotated[int, Field(ge=0, le=10**12, description="Minor units (pence)")]
RateBp = Annotated[int, Field(ge=0, le=10_000, description="Basis points: 2000 = 20.00%")]
VatCode = Literal["standard", "reduced", "zero", "exempt", "outside_scope"]
SequenceKind = Literal["client", "job", "invoice", "credit_note", "staff", "ticket", "estimate"]
SettingsCode = Annotated[
    str,
    Field(
        min_length=1,
        max_length=12,
        description="Upper-cased; letters, digits, '_' and '-' (1-12)",
    ),
]
_SETTINGS_CODE = r"^[A-Z0-9_-]{1,12}$"


def reject_nulls(model: BaseModel, *fields: str) -> None:
    """A PATCH field may be left out, but only nullable columns accept an explicit null."""
    for field in fields:
        if field in model.model_fields_set and getattr(model, field) is None:
            raise ValueError(f"{field}: This field cannot be null")


def blank_is_null(value: str | None) -> str | None:
    return value or None


def upper_code(value: str | None) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if not re.fullmatch(_SETTINGS_CODE, code):
        raise ValueError("Use 1-12 letters, digits, '_' or '-'")
    return code


class DeleteOutcome(BaseModel):
    """What a DELETE did: removed the row, or kept it archived/deactivated because it is used."""

    outcome: Literal["deleted", "archived", "deactivated"]
    id: uuid.UUID


# --- VAT rates --------------------------------------------------------------------------


class VatRateRead(BaseModel):
    id: uuid.UUID
    name: str
    code: str
    rate_bp: int
    is_default: bool
    is_archived: bool
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, rate: VatRate) -> "VatRateRead":
        return cls(
            id=rate.id,
            name=rate.name,
            code=rate.code,
            rate_bp=rate.rate_bp,
            is_default=rate.is_default,
            is_archived=rate.archived_at is not None,
            archived_at=rate.archived_at,
            created_at=rate.created_at,
            updated_at=rate.updated_at,
        )


class VatRateCreate(RequestModel):
    name: str = Field(min_length=1, max_length=60)
    code: VatCode
    rate_bp: RateBp
    is_default: bool = False


class VatRateUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    code: VatCode | None = None
    rate_bp: RateBp | None = None
    # true makes this the default (the old default is unset); false leaves no default.
    is_default: bool | None = None
    # false restores an archived rate; true archives it (same as DELETE on a used rate).
    is_archived: bool | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, *type(self).model_fields)
        return self


# --- Services ---------------------------------------------------------------------------

EntityStatus = Literal["active", "inactive"]
InvoiceDescription = Annotated[str, Field(max_length=500)]


class ServiceRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    billing_basis: str
    default_fee: int | None
    # Whether work on the service is billable by default: jobs created without a billing
    # basis start as non_billable when false.
    default_billable: bool
    vat_rate_id: uuid.UUID | None
    invoice_description: str | None
    is_active: bool
    status: EntityStatus
    sort_order: int
    # The activity codes time on this service's jobs is limited to; empty = every code.
    activity_code_ids: list[uuid.UUID]
    # True once jobs, client engagements, invoice lines or rates refer to the service.
    is_used: bool
    # Used services can only be deactivated: DELETE does that instead.
    can_delete: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(
        cls, service: Service, *, activity_code_ids: list[uuid.UUID], is_used: bool
    ) -> "ServiceRead":
        return cls(
            id=service.id,
            code=service.code,
            name=service.name,
            description=service.description,
            billing_basis=service.billing_basis,
            default_fee=service.default_fee,
            default_billable=service.default_billable,
            vat_rate_id=service.vat_rate_id,
            invoice_description=service.invoice_description,
            is_active=service.is_active,
            status="active" if service.is_active else "inactive",
            sort_order=service.sort_order,
            activity_code_ids=activity_code_ids,
            is_used=is_used,
            can_delete=not is_used,
            created_at=service.created_at,
            updated_at=service.updated_at,
        )


class ServiceCreate(RequestModel):
    code: SettingsCode
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    billing_basis: Literal["hourly", "fixed"] = "hourly"
    default_fee: Money | None = None
    default_billable: bool = True
    vat_rate_id: uuid.UUID | None = None
    invoice_description: InvoiceDescription | None = None
    sort_order: int = Field(default=0, ge=0, le=10_000)
    is_active: bool = True

    _code = field_validator("code")(upper_code)
    _description = field_validator("description", "invoice_description")(blank_is_null)


class ServiceUpdate(RequestModel):
    code: SettingsCode | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    billing_basis: Literal["hourly", "fixed"] | None = None
    default_fee: Money | None = None
    default_billable: bool | None = None
    vat_rate_id: uuid.UUID | None = None
    invoice_description: InvoiceDescription | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
    is_active: bool | None = None

    _code = field_validator("code")(upper_code)
    _description = field_validator("description", "invoice_description")(blank_is_null)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(
            self, "code", "name", "billing_basis", "default_billable", "sort_order", "is_active"
        )
        return self


# --- Activity codes ---------------------------------------------------------------------


class ActivityCodeRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    # The default billable status of time recorded against the code.
    is_chargeable: bool
    vat_rate_id: uuid.UUID | None
    invoice_description: str | None
    is_active: bool
    status: EntityStatus
    # True once time entries or timers use the code, or a service lists it.
    is_used: bool
    can_delete: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, activity: ActivityCode, *, is_used: bool) -> "ActivityCodeRead":
        return cls(
            id=activity.id,
            code=activity.code,
            name=activity.name,
            is_chargeable=activity.is_chargeable,
            vat_rate_id=activity.vat_rate_id,
            invoice_description=activity.invoice_description,
            is_active=activity.is_active,
            status="active" if activity.is_active else "inactive",
            is_used=is_used,
            can_delete=not is_used,
            created_at=activity.created_at,
            updated_at=activity.updated_at,
        )


class ActivityCodeCreate(RequestModel):
    code: SettingsCode
    name: str = Field(min_length=1, max_length=120)
    is_chargeable: bool = True
    vat_rate_id: uuid.UUID | None = None
    invoice_description: InvoiceDescription | None = None
    is_active: bool = True

    _code = field_validator("code")(upper_code)
    _description = field_validator("invoice_description")(blank_is_null)


class ActivityCodeUpdate(RequestModel):
    code: SettingsCode | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_chargeable: bool | None = None
    vat_rate_id: uuid.UUID | None = None
    invoice_description: InvoiceDescription | None = None
    is_active: bool | None = None

    _code = field_validator("code")(upper_code)
    _description = field_validator("invoice_description")(blank_is_null)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, "code", "name", "is_chargeable", "is_active")
        return self


class ServiceActivityCodesRead(BaseModel):
    service_id: uuid.UUID
    # False: no limit, every activity code can be used on the service's jobs.
    restricted: bool
    activity_codes: list[ActivityCodeRead]


class ServiceActivityCodesUpdate(RequestModel):
    activity_code_ids: list[uuid.UUID] = Field(
        max_length=200,
        description="The activity codes to allow; an empty list removes the limit (all allowed)",
    )


# --- Number sequences -------------------------------------------------------------------


class SequenceRead(BaseModel):
    kind: str
    prefix: str
    padding: int
    next_number: int
    # How the next number will look, e.g. "INV-00042".
    preview: str

    @classmethod
    def from_model(cls, sequence: DocumentSequence) -> "SequenceRead":
        return cls(
            kind=sequence.kind,
            prefix=sequence.prefix,
            padding=sequence.padding,
            next_number=sequence.next_number,
            preview=format_number(sequence.prefix, sequence.next_number, sequence.padding),
        )


class SequenceUpdate(RequestModel):
    prefix: str | None = Field(default=None, max_length=12, pattern=r"^[A-Za-z0-9/_-]{0,12}$")
    padding: int | None = Field(default=None, ge=1, le=10)
    next_number: int | None = Field(default=None, ge=1, le=2_000_000_000)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, *type(self).model_fields)
        return self

"""Request and response models of leads: prospective clients and what winning them cost."""

import uuid
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.db.models import Client, Lead
from app.schemas.billing import ClientRef
from app.schemas.clients import ClientType, normalise_code
from app.schemas.common import RequestModel
from app.schemas.settings import Money, blank_is_null, reject_nulls

LeadStatus = Literal["open", "converted", "archived"]
_TEXT_FIELDS = ("contact_name", "phone", "source", "notes")


def status_of(lead: Lead) -> LeadStatus:
    """Archived wins: an archived lead is hidden whether or not it was converted."""
    if lead.archived_at is not None:
        return "archived"
    if lead.converted_client_id is not None:
        return "converted"
    return "open"


class LeadRead(BaseModel):
    id: uuid.UUID
    name: str
    client_type: str
    contact_name: str | None
    email: str | None
    phone: str | None
    source: str | None
    notes: str | None
    acquisition_cost: int = Field(description="Pence")
    onboarding_cost: int = Field(description="Pence")
    total_cost: int = Field(description="acquisition_cost + onboarding_cost, in pence")
    status: LeadStatus = Field(description="`archived` wins over `converted`")
    archived_at: datetime | None
    converted_at: datetime | None
    converted_client_id: uuid.UUID | None
    converted_client: ClientRef | None = Field(
        description="The client the lead became; null until converted and once it is deleted"
    )
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, lead: Lead, client: Client | None = None) -> "LeadRead":
        return cls(
            id=lead.id,
            name=lead.name,
            client_type=lead.client_type,
            contact_name=lead.contact_name,
            email=lead.email,
            phone=lead.phone,
            source=lead.source,
            notes=lead.notes,
            acquisition_cost=lead.acquisition_cost,
            onboarding_cost=lead.onboarding_cost,
            total_cost=lead.acquisition_cost + lead.onboarding_cost,
            status=status_of(lead),
            archived_at=lead.archived_at,
            converted_at=lead.converted_at,
            converted_client_id=lead.converted_client_id,
            converted_client=ClientRef.from_model(client) if client is not None else None,
            created_by=lead.created_by,
            created_at=lead.created_at,
            updated_at=lead.updated_at,
        )


class LeadTotals(BaseModel):
    count: int
    acquisition_cost: int = Field(description="Pence")
    onboarding_cost: int = Field(description="Pence")
    total_cost: int = Field(description="Pence")


class LeadSummary(BaseModel):
    open: LeadTotals
    converted: LeadTotals
    archived: LeadTotals


class _LeadFields(RequestModel):
    """Validation shared by create and update."""

    _blank = field_validator(*_TEXT_FIELDS, check_fields=False)(blank_is_null)

    @field_validator("email", mode="before", check_fields=False)
    @classmethod
    def _blank_email(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


class LeadCreate(_LeadFields):
    name: str = Field(min_length=1, max_length=200, description="Becomes the client's name")
    client_type: ClientType = "limited_company"
    contact_name: str | None = Field(
        default=None, max_length=120, description="Becomes the client's primary contact"
    )
    email: EmailStr | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    source: str | None = Field(
        default=None, max_length=100, description="Where the lead came from: Referral, ..."
    )
    notes: str | None = Field(default=None, max_length=5000)
    acquisition_cost: Money = 0
    onboarding_cost: Money = 0


class LeadUpdate(_LeadFields):
    """Every field optional; the text fields accept null to clear them."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    client_type: ClientType | None = None
    contact_name: str | None = Field(default=None, max_length=120)
    email: EmailStr | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    source: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=5000)
    acquisition_cost: Money | None = None
    onboarding_cost: Money | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, "name", "client_type", "acquisition_cost", "onboarding_cost")
        return self


class LeadConvert(RequestModel):
    code: str | None = Field(
        default=None,
        max_length=20,
        description="The new client's code; generated from the client sequence if omitted",
    )

    @field_validator("code")
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        return normalise_code(value)

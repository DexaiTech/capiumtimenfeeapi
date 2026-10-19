"""Request and response models of the V1 rate endpoints: a person's staff rates and a client's
rates. They are views of the dated rate rules (``app/schemas/rate_rules.py`` has the full model)."""

import uuid
from datetime import date, datetime
from typing import Self

from pydantic import BaseModel, Field, model_validator

from app.db.models import OrganizationMember, RateRule, User
from app.repositories.rates import StaffRateRow
from app.schemas.common import RequestModel
from app.schemas.settings import Money


class StaffRateRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    effective_from: date
    # The day before the person's next rate starts; null for the latest.
    effective_to: date | None
    # Minor units per hour.
    charge_rate: int
    # Null for a caller without ``rate.read`` (e.g. a custom role with only ``rate.manage``).
    cost_rate: int | None
    created_by: uuid.UUID | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: StaffRateRow, *, show_cost: bool) -> "StaffRateRead":
        return cls(
            id=row.id,
            user_id=row.user_id,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
            charge_rate=row.charge_rate,
            cost_rate=row.cost_rate if show_cost else None,
            created_by=row.created_by,
            created_at=row.created_at,
        )


class StaffRateCreate(RequestModel):
    effective_from: date
    charge_rate: Money = Field(description="Charge-out rate, minor units per hour")
    cost_rate: Money = Field(default=0, description="Cost rate, minor units per hour")


class CurrentStaffRate(BaseModel):
    """An active member and the rate in force for them on ``on_date`` (null when none)."""

    user_id: uuid.UUID
    full_name: str
    email: str
    initials: str | None
    job_title: str | None
    on_date: date
    rate_id: uuid.UUID | None
    effective_from: date | None
    charge_rate: int | None
    cost_rate: int | None

    @classmethod
    def build(
        cls,
        member: OrganizationMember,
        user: User,
        charge: RateRule | None,
        cost: RateRule | None,
        on_date: date,
    ) -> "CurrentStaffRate":
        return cls(
            user_id=member.user_id,
            full_name=user.full_name,
            email=user.email if member.login_enabled else (member.work_email or ""),
            initials=member.initials,
            job_title=member.job_title,
            on_date=on_date,
            rate_id=charge.id if charge else None,
            effective_from=charge.effective_from if charge else None,
            charge_rate=charge.rate if charge else None,
            cost_rate=cost.rate if cost else (0 if charge else None),
        )


class ClientRateRead(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    # Null: the rate applies to everyone working for the client.
    user_id: uuid.UUID | None
    user_name: str | None
    hourly_rate: int
    effective_from: date
    effective_to: date | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, rate: RateRule, user_name: str | None) -> "ClientRateRead":
        assert rate.client_id is not None
        return cls(
            id=rate.id,
            client_id=rate.client_id,
            user_id=rate.user_id,
            user_name=user_name,
            hourly_rate=rate.rate,
            effective_from=rate.effective_from,
            effective_to=rate.effective_to,
            created_at=rate.created_at,
            updated_at=rate.updated_at,
        )


class ClientRateCreate(RequestModel):
    user_id: uuid.UUID | None = Field(default=None, description="Null = everyone")
    hourly_rate: Money = Field(description="Minor units per hour")
    effective_from: date | None = Field(
        default=None, description="Default 2000-01-01: the rate applies to every day"
    )
    effective_to: date | None = None

    @model_validator(mode="after")
    def _period(self) -> Self:
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("effective_to: Must not be before effective_from")
        return self


class ClientRateUpdate(RequestModel):
    hourly_rate: Money = Field(description="Minor units per hour")

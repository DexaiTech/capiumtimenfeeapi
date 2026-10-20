"""Request and response models of recurring invoice schedules."""

import uuid
from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.models import Client, RecurringLine, RecurringRun, RecurringSchedule
from app.schemas.billing import ClientRef
from app.schemas.common import RequestModel
from app.schemas.estimates import LineInput, Quantity, SignedMoney

Frequency = Literal["weekly", "monthly", "quarterly", "annually"]
ScheduleStatus = Literal["active", "paused", "ended"]
RunStatus = Literal["succeeded", "failed"]


def _blank_is_null(value: str | None) -> str | None:
    return value or None


class ScheduleCreate(RequestModel):
    client_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    frequency: Frequency
    start_date: date = Field(description="The first invoice is drafted on this date")
    end_date: date | None = Field(default=None, description="No invoice after this date")
    reference: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[LineInput] = Field(min_length=1, max_length=200)

    _blank = field_validator("reference", "notes")(_blank_is_null)

    @model_validator(mode="after")
    def _dates(self) -> Self:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date: The end date is before the start date")
        return self


class ScheduleUpdate(RequestModel):
    """``lines``, when sent, replaces every line. The start date and client cannot change."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    frequency: Frequency | None = None
    end_date: date | None = None
    reference: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[LineInput] | None = Field(default=None, min_length=1, max_length=200)

    _blank = field_validator("reference", "notes")(_blank_is_null)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        for name in ("name", "frequency", "lines"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name}: Cannot be null")
        return self


class ScheduleLineRead(BaseModel):
    id: uuid.UUID
    position: int
    description: str
    job_id: uuid.UUID | None
    service_id: uuid.UUID | None
    quantity: Quantity
    unit_amount: SignedMoney
    vat_rate_id: uuid.UUID | None

    @classmethod
    def from_model(cls, line: RecurringLine) -> Self:
        return cls(
            id=line.id,
            position=line.position,
            description=line.description,
            job_id=line.job_id,
            service_id=line.service_id,
            quantity=line.quantity,
            unit_amount=line.unit_amount,
            vat_rate_id=line.vat_rate_id,
        )


class ScheduleSummaryRead(BaseModel):
    id: uuid.UUID
    name: str
    client: ClientRef | None
    frequency: Frequency
    status: ScheduleStatus
    start_date: date
    end_date: date | None
    next_run_date: date | None = Field(description="Null once the schedule has ended")
    last_run_on: date | None
    reference: str | None
    net_estimate: int = Field(description="Net of one invoice, before the VAT of the day")
    failed_runs: int = Field(description="Runs waiting for a retry")
    created_at: datetime

    @classmethod
    def build(
        cls,
        schedule: RecurringSchedule,
        client: Client | None,
        *,
        net_estimate: int,
        failed_runs: int,
    ) -> Self:
        return cls(
            id=schedule.id,
            name=schedule.name,
            client=ClientRef.from_model(client) if client else None,
            frequency=schedule.frequency,
            status=schedule.status,
            start_date=schedule.start_date,
            end_date=schedule.end_date,
            next_run_date=None if schedule.status == "ended" else schedule.next_run_date,
            last_run_on=schedule.last_run_on,
            reference=schedule.reference,
            net_estimate=net_estimate,
            failed_runs=failed_runs,
            created_at=schedule.created_at,
        )


class ScheduleRead(ScheduleSummaryRead):
    notes: str | None
    lines: list[ScheduleLineRead]


class RunRead(BaseModel):
    id: uuid.UUID
    schedule_id: uuid.UUID
    run_date: date
    status: RunStatus
    invoice_id: uuid.UUID | None
    invoice_number: str | None = None
    error: str | None
    attempts: int
    ran_at: datetime

    @classmethod
    def build(cls, run: RecurringRun, invoice_number: str | None = None) -> Self:
        return cls(
            id=run.id,
            schedule_id=run.schedule_id,
            run_date=run.run_date,
            status=run.status,
            invoice_id=run.invoice_id,
            invoice_number=invoice_number,
            error=run.error,
            attempts=run.attempts,
            ran_at=run.ran_at,
        )

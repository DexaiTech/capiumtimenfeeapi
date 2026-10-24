"""Jobs created from client engagements: the next period and the run history."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.jobs import JobRead


class NextJobPeriod(BaseModel):
    """The period whose job is created next."""

    period_start: date
    period_end: date | None = Field(description="Null for a one-off engagement")
    create_on: date = Field(
        description="The day the worker creates the job: the period start minus the lead days"
    )


class EngagementJobCreated(BaseModel):
    job: JobRead
    period_start: date
    period_end: date | None


class EngagementJobRef(BaseModel):
    id: uuid.UUID
    number: str
    name: str


class EngagementJobRunRead(BaseModel):
    period_start: date
    period_end: date | None
    status: Literal["created", "failed"]
    error: str | None = Field(description="Why a failed run failed")
    job: EngagementJobRef | None = Field(description="Null when the job was deleted")
    created_at: datetime

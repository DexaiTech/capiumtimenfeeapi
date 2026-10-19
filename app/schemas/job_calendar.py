"""Response models of the job calendar."""

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.job_tasks import AssigneeRef

# A job's status or a task's: ``not_started`` is a task's word for a job's ``planned``.
CalendarStatus = Literal[
    "not_started", "planned", "in_progress", "on_hold", "completed", "closed", "cancelled"
]


class CalendarEvent(BaseModel):
    type: Literal["job", "task"]
    id: uuid.UUID = Field(description="The job's or the task's id")
    job_id: uuid.UUID
    job_number: str
    title: str = Field(description="The job's name, or the task's title")
    client_id: uuid.UUID
    client_name: str
    start: date = Field(description="First day to draw")
    end: date | None = Field(
        description="Last day to draw; null for an open job with a start date and no due date"
    )
    status: str = Field(description="The job's status (planned...) or the task's (not_started...)")
    due: date | None = Field(description="The due date; for a task the day it is drawn on")
    assignees: list[AssigneeRef] = Field(description="The job's or the task's assignees")
    budget_used_bp: int | None = Field(
        description="A job: the worse of its hours and fee usage. A task: logged / budgeted "
        "minutes. Basis points; null without a budget"
    )


class JobCalendar(BaseModel):
    range_from: date = Field(serialization_alias="from")
    range_to: date = Field(serialization_alias="to")
    events: list[CalendarEvent]

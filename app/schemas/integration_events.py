"""Request and response models of the central user and practice management events, the change feeds
and the mock services' controls."""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.schemas.common import RequestModel
from app.services.integration_events import Handled, Polled

EventId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=100,
        description="The event's id: unique and stable, so delivering it again changes nothing",
    ),
]
PmId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$",
        description="An id at practice management",
    ),
]
CentralUserEventType = Literal["renamed", "deactivated", "reactivated"]
PmEventType = Literal["job_updated", "job_status_changed", "task_status_changed"]
PmJobStatus = Literal["not_started", "in_progress", "on_hold", "completed", "closed", "cancelled"]
PmTaskStatus = Literal["not_started", "in_progress", "on_hold", "completed"]
EventOutcome = Literal["applied", "skipped", "failed"]
JobOutcome = Literal["created", "linked", "updated", "unchanged", "skipped", "failed"]


class EventResult(BaseModel):
    event_id: str = Field(description="The event's id")
    type: str = Field(description="The event's type")
    outcome: EventOutcome = Field(
        description=(
            "`applied`; `skipped` (nothing to do here, or already processed); `failed` (a rule of "
            "this practice refused it: it is kept as a failed sync item, see `item_id`)"
        )
    )
    duplicate: bool = Field(description="The event had already been processed: nothing changed")
    message: str = Field(description="What happened, in words")
    entity_id: uuid.UUID | None = Field(
        description="The member or job the event changed (or would have)"
    )
    item_id: uuid.UUID | None = Field(description="The failed sync item that keeps a refused event")

    @classmethod
    def from_handled(cls, handled: Handled) -> "EventResult":
        return cls(
            event_id=handled.event_id,
            type=handled.type,
            outcome=handled.outcome,
            duplicate=handled.duplicate,
            message=handled.message,
            entity_id=handled.entity_id,
            item_id=handled.item_id,
        )


class ProcessedFeed(BaseModel):
    available: bool = Field(description="The other system answered")
    fetched: int = Field(description="Events read from the change feed")
    applied: int = Field(description="Events applied")
    skipped: int = Field(description="Events with nothing to do (or repeats)")
    failed: int = Field(
        description=(
            "Events refused (kept as failed sync items) or that raised and stopped the batch"
        )
    )
    has_more: bool = Field(description="More events are waiting: call again")
    cursor: str | None = Field(description="Where this practice has read the feed up to")
    events: list[EventResult] = Field(description="One entry per event handled")

    @classmethod
    def from_polled(cls, polled: Polled) -> "ProcessedFeed":
        return cls(
            available=polled.available,
            fetched=polled.fetched,
            applied=polled.applied,
            skipped=polled.skipped,
            failed=polled.failed,
            has_more=polled.has_more,
            cursor=polled.cursor,
            events=[EventResult.from_handled(handled) for handled in polled.results],
        )


# --- Central user --------------------------------------------------------------------------


class CentralUserEventRequest(RequestModel):
    event_id: EventId
    type: CentralUserEventType = Field(description="`renamed`, `deactivated` or `reactivated`")
    email: EmailStr = Field(
        max_length=254, description="The person's email address: how they are found in the practice"
    )
    full_name: str | None = Field(
        default=None, min_length=1, max_length=120, description="The new name (`renamed`)"
    )
    central_user_id: str | None = Field(
        default=None, max_length=64, description="The person's id at the central user service"
    )
    occurred_at: datetime | None = Field(default=None, description="When it happened")

    @model_validator(mode="after")
    def _name_for_renames(self) -> Self:
        if self.type == "renamed" and not self.full_name:
            raise ValueError("full_name: A rename carries the new name")
        if self.type != "renamed" and self.full_name is not None:
            raise ValueError("full_name: Only a rename carries a name")
        return self


class CentralUserMockChange(RequestModel):
    type: CentralUserEventType = Field(description="`renamed`, `deactivated` or `reactivated`")
    email: EmailStr = Field(max_length=254, description="The person's email address")
    full_name: str | None = Field(
        default=None, min_length=1, max_length=120, description="The new name (`renamed`)"
    )

    @model_validator(mode="after")
    def _name_for_renames(self) -> Self:
        if self.type == "renamed" and not self.full_name:
            raise ValueError("full_name: A rename carries the new name")
        return self


# --- Practice management ------------------------------------------------------------------


class PmEventRequest(RequestModel):
    event_id: EventId
    type: PmEventType = Field(
        description=(
            "`job_updated` (read the job again), `job_status_changed` or `task_status_changed`"
        )
    )
    pm_job_id: PmId
    pm_task_id: PmId | None = Field(default=None, description="The task (`task_status_changed`)")
    status: str | None = Field(
        default=None,
        max_length=30,
        description="The new status (job: `not_started`, `in_progress`, `on_hold`, `completed`, "
        "`closed`, `cancelled`; task: the first four)",
    )
    occurred_at: datetime | None = Field(default=None, description="When it happened")

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.type == "job_updated" and (self.status is not None or self.pm_task_id is not None):
            raise ValueError("status: A job update carries no status or task")
        if self.type == "job_status_changed" and (not self.status or self.pm_task_id is not None):
            raise ValueError("status: A job status change carries the new status and no task")
        if self.type == "task_status_changed" and (not self.status or self.pm_task_id is None):
            raise ValueError("pm_task_id: A task status change carries the task and its status")
        return self


class PmMockChange(RequestModel):
    """A change someone makes in the mock practice management system."""

    type: PmEventType = Field(
        description=(
            "`job_updated` (change the fields given), `job_status_changed`, `task_status_changed`"
        )
    )
    pm_job_id: PmId
    pm_task_id: PmId | None = Field(
        default=None, description="The task (`task_status_changed`)"
    )
    status: str | None = Field(default=None, max_length=30, description="The new status")
    name: str | None = Field(
        default=None, min_length=1, max_length=200, description="`job_updated`"
    )
    service_code: str | None = Field(default=None, max_length=12, description="`job_updated`")
    manager_email: EmailStr | None = Field(default=None, description="`job_updated`")
    partner_email: EmailStr | None = Field(default=None, description="`job_updated`")
    start_date: date | None = Field(default=None, description="`job_updated`")
    due_date: date | None = Field(default=None, description="`job_updated`")


class MockChanged(BaseModel):
    event_id: str = Field(description="The event the change appended to the mock's feed")
    type: str = Field(description="The event's type")


class PmJobResult(BaseModel):
    pm_job_id: str = Field(description="The job at practice management")
    job_id: uuid.UUID | None = Field(description="The job of this practice, when there is one")
    job_number: str | None = Field(description="Its number")
    name: str = Field(description="The job's name at practice management")
    outcome: JobOutcome = Field(
        description=(
            "`created` (a new job), `linked` (an existing job of the same client and name was "
            "linked), `updated`, `unchanged`, `skipped` (see `message`) or `failed`"
        )
    )
    message: str = Field(description="What happened, in words")
    warnings: list[str] = Field(description="Things that were not applied, e.g. an unknown manager")


class PmImportResult(BaseModel):
    available: bool = Field(description="Practice management answered")
    processed: int = Field(description="PM jobs handled by this call")
    created: int = Field(description="Jobs created")
    linked: int = Field(description="Existing jobs linked")
    updated: int = Field(description="Linked jobs that changed")
    unchanged: int = Field(description="Linked jobs that were already up to date")
    skipped: int = Field(description="PM jobs not imported (client not enabled here, ...)")
    failed: int = Field(description="PM jobs that could not be handled")
    has_more: bool = Field(description="More PM jobs are waiting: call again with `next_cursor`")
    next_cursor: str | None = Field(description="Pass as `cursor` to continue")
    results: list[PmJobResult] = Field(description="One entry per PM job handled")


class PmImportRequest(RequestModel):
    cursor: str | None = Field(
        default=None, max_length=64, description="From a previous call's `next_cursor`"
    )
    limit: int = Field(default=100, ge=1, le=100, description="PM jobs to handle in this call")

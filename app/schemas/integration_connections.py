"""Request and response models of the integration cards, the sync items and the mock controls."""

import uuid
from datetime import date, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import IntegrationSyncItem
from app.schemas.common import RequestModel

IntegrationKey = Literal["central_client", "central_user", "practice_management", "bookkeeping"]
CardStatus = Literal["disabled", "ready", "connected", "error"]
ItemStatus = Literal["pending", "ok", "failed", "skipped"]
ItemDirection = Literal["inbound", "outbound"]
RetryOutcome = Literal["sent", "retry_scheduled", "failed", "skipped", "already_synced"]


class _Config(BaseModel):
    """The settings of one integration: unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")


class NoConfig(_Config):
    """The central integrations have no settings."""


class PracticeManagementConfig(_Config):
    sync_statuses: bool = Field(
        default=True,
        description="Keep job and task statuses in step with practice management, both ways",
    )


class BookkeepingConfig(_Config):
    send_invoices: bool = Field(default=True, description="Send issued invoices")
    send_credit_notes: bool = Field(default=True, description="Send credit notes")
    send_from: date | None = Field(
        default=None,
        description="Send only documents issued on or after this date (null: all of them)",
    )


CONFIG_MODELS: dict[str, type[_Config]] = {
    "central_client": NoConfig,
    "central_user": NoConfig,
    "practice_management": PracticeManagementConfig,
    "bookkeeping": BookkeepingConfig,
}


class IntegrationMock(BaseModel):
    available: bool = Field(description="Whether the mock service answers (false: an outage)")


class IntegrationCard(BaseModel):
    key: IntegrationKey = Field(description="`central_client`, `central_user`, `practice_management` or `bookkeeping`")
    name: str = Field(description="The integration's name")
    description: str = Field(description="What it does, in a sentence")
    phase: str | None = Field(description="`2A` (practice management) or `2B` (bookkeeping), else null")
    enabled: bool = Field(description="Switched on for this practice")
    always_on: bool = Field(description="Cannot be switched off (the central client service)")
    status: CardStatus = Field(
        description=(
            "`disabled`, `ready` (on, nothing synchronised yet), `connected` (the last attempt "
            "worked) or `error` (the last attempt failed, or items are failed)"
        )
    )
    last_success_at: datetime | None = Field(description="When it last worked")
    last_attempt_at: datetime | None = Field(description="When it was last tried")
    last_error: str | None = Field(description="Why the last attempt failed, in words")
    pending_items: int = Field(description="Sync items waiting to be sent or retried")
    failed_items: int = Field(description="Sync items that failed and need a person")
    config: dict[str, Any] = Field(description="The settings, with their defaults filled in")
    summary: dict[str, int] = Field(
        description="Counts specific to the integration (linked clients, linked jobs, ...)"
    )
    mock: IntegrationMock | None = Field(
        description="Only while a mock service is in use (never in production)"
    )


class IntegrationUpdate(RequestModel):
    enabled: bool | None = Field(default=None, description="Switch the integration on or off")
    config: dict[str, Any] | None = Field(
        default=None,
        description="Settings to change; those left out keep their value (see the card's `config`)",
    )


class SyncItemRead(BaseModel):
    id: uuid.UUID = Field(description="The sync item")
    integration: IntegrationKey = Field(description="Which integration it belongs to")
    entity_type: str | None = Field(
        description="The kind of record: `invoice`, `credit_note`, `job`, `task` or `member`"
    )
    entity_id: uuid.UUID | None = Field(description="The record in this practice")
    label: str | None = Field(description="A name for the record, e.g. the invoice number")
    direction: ItemDirection = Field(description="`outbound`: sent to the other system")
    operation: str = Field(description="What is done, e.g. `send_invoice` or `push_job_status`")
    status: ItemStatus = Field(
        description="`pending` (waiting, or waiting to be retried), `ok`, `failed` or `skipped`"
    )
    attempts: int = Field(description="Attempts made so far")
    max_attempts: int = Field(description="Attempts after which a failing item is `failed`")
    next_attempt_at: datetime | None = Field(description="When a `pending` item is next tried")
    last_attempt_at: datetime | None = Field(description="When it was last tried")
    completed_at: datetime | None = Field(description="When it was sent or skipped")
    last_error: str | None = Field(description="Why the last attempt failed, in words")
    last_error_code: str | None = Field(
        description="A stable word for the failure: `UNMAPPED`, `UNAVAILABLE`, `REJECTED`, ..."
    )
    external_ref: str | None = Field(description="The other system's reference once it is `ok`")
    payload: dict[str, Any] = Field(description="What the item carries (for support)")
    created_at: datetime = Field(description="When it was queued")
    updated_at: datetime = Field(description="When it last changed")

    @classmethod
    def from_model(cls, item: IntegrationSyncItem, *, max_attempts: int) -> "SyncItemRead":
        label = item.payload.get("label")
        return cls(
            id=item.id,
            integration=cast(IntegrationKey, item.integration),
            entity_type=item.entity_type,
            entity_id=item.entity_id,
            label=label if isinstance(label, str) else None,
            direction=cast(ItemDirection, item.direction),
            operation=item.operation,
            status=cast(ItemStatus, item.status),
            attempts=item.attempts,
            max_attempts=max_attempts,
            next_attempt_at=item.next_attempt_at,
            last_attempt_at=item.last_attempt_at,
            completed_at=item.completed_at,
            last_error=item.last_error,
            last_error_code=item.last_error_code,
            external_ref=item.external_ref,
            payload=item.payload,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


class SyncRunResult(BaseModel):
    integration: IntegrationKey = Field(description="The integration that was run")
    available: bool = Field(description="The other system answered (false: it was unavailable)")
    queued: int = Field(description="New sync items queued by this run (documents found)")
    processed: int = Field(description="Sync items attempted by this run")
    ok: int = Field(description="Items sent or applied")
    retrying: int = Field(description="Items that failed and will be tried again")
    failed: int = Field(description="Items that failed and need a person")
    skipped: int = Field(description="Items with nothing to do")
    pending: int = Field(description="Items still waiting after this run")
    details: dict[str, int] = Field(
        description="Counts specific to the integration (jobs imported, events applied, ...)"
    )
    message: str = Field(description="What happened, in words")


class SyncItemRetried(BaseModel):
    outcome: RetryOutcome = Field(
        description=(
            "`sent` (now ok), `retry_scheduled` (failed again, will be retried), `failed` (failed "
            "again, needs a person), `skipped` or `already_synced` (nothing was sent again)"
        )
    )
    item: SyncItemRead = Field(description="The item after the attempt")


class RetryFailedResult(BaseModel):
    requeued: int = Field(description="Failed items put back to work (at most 200 per call)")
    ok: int = Field(description="Of those, sent or applied now")
    failed: int = Field(description="Of those, failed again")
    remaining: int = Field(description="Failed items left for another call")


class MockAvailabilityUpdate(RequestModel):
    available: bool = Field(description="`false` makes the mock service answer as if it were down")


class MockAvailabilityRead(BaseModel):
    integration: IntegrationKey = Field(description="Which mock service")
    available: bool = Field(description="Whether the mock service answers")

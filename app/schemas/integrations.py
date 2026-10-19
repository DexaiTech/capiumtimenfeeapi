"""Response models of the integration log."""

import uuid
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, Field

from app.db.models import IntegrationSyncLog

SyncStatus = Literal["ok", "failed", "skipped"]
SyncDirection = Literal["inbound", "outbound"]


class SyncLogRead(BaseModel):
    id: uuid.UUID = Field(description="The log entry")
    integration: str = Field(description="Which integration, e.g. `central_client`")
    direction: SyncDirection = Field(description="`inbound`: data coming in from the other system")
    operation: str = Field(
        description=(
            "What was attempted: `enable`, `link`, `unlink`, `sync`, `renamed`, `archived`, "
            "`restored`, `merged`, `poll` or `event`"
        )
    )
    entity_type: str | None = Field(description="The kind of local record, e.g. `client`")
    entity_id: uuid.UUID | None = Field(description="The local record")
    external_id: str | None = Field(description="The record's id in the other system")
    external_event_id: str | None = Field(
        description="Set on the row that recorded an event as processed (`ok` or `skipped`)"
    )
    status: SyncStatus = Field(description="`ok`, `failed` or `skipped`")
    message: str | None = Field(description="What happened, in words")
    attempt: int = Field(description="1, then one more for each failed attempt in a row")
    details: dict[str, Any] | None = Field(
        description="What the attempt carried or changed (field changes, counts, the error)"
    )
    created_at: datetime = Field(description="When it was attempted")

    @classmethod
    def from_model(cls, entry: IntegrationSyncLog) -> "SyncLogRead":
        return cls(
            id=entry.id,
            integration=entry.integration,
            direction=cast(SyncDirection, entry.direction),
            operation=entry.operation,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            external_id=entry.external_id,
            external_event_id=entry.external_event_id,
            status=cast(SyncStatus, entry.status),
            message=entry.message,
            attempt=entry.attempt,
            details=entry.details,
            created_at=entry.created_at,
        )

"""Request filters and response models of the audit-log endpoints."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


@dataclass(frozen=True, slots=True)
class AuditFilters:
    """What the list, the export and the record history narrow the entries by."""

    action: str | None = None
    actor_user_id: uuid.UUID | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    target_type: str | None = None
    target_id: uuid.UUID | None = None
    q: str | None = None


class AuditChange(BaseModel):
    """One field of a record that an action changed."""

    field: str = Field(description="The field, as the service names it (`status`, `rate`)")
    before: Any = Field(default=None, description="The value before; null when there was none")
    after: Any = Field(default=None, description="The value after; null when it was cleared")


class AuditActor(BaseModel):
    id: uuid.UUID = Field(description="The user")
    full_name: str = Field(description="Their name")
    email: str = Field(description="Their email address")


class AuditLogEntryRead(BaseModel):
    id: uuid.UUID = Field(description="The entry")
    created_at: datetime = Field(description="When it was written (UTC)")
    action: str = Field(description="`<area>.<what happened>`, e.g. `invoice.issued`")
    label: str = Field(description="The action in words, e.g. `Invoice issued`")
    summary: str = Field(description="The label with the record it is about")
    actor: AuditActor | None = Field(description="Who did it; null for what the system did")
    target_type: str | None = Field(description="The kind of record it is about (`client` ...)")
    target_id: uuid.UUID | None = Field(description="The record it is about")
    ip_address: str | None = Field(description="The caller's address")
    user_agent: str | None = Field(description="The caller's browser or client")
    changes: list[AuditChange] = Field(
        description="What the action changed, normalised from the stored details; may be empty"
    )
    metadata: dict[str, Any] = Field(
        description="The details the service recorded; cost figures are null without `rate.read`"
    )


class AuditHistoryActor(BaseModel):
    id: uuid.UUID = Field(description="The user")
    name: str = Field(description="Their name")


class AuditHistoryEntryRead(BaseModel):
    """One entry of a record's history."""

    id: uuid.UUID = Field(description="The entry")
    action: str = Field(description="`<area>.<what happened>`, e.g. `invoice.issued`")
    label: str = Field(description="The action in words, e.g. `Invoice issued`")
    actor: AuditHistoryActor | None = Field(description="Null for what the system did")
    at: datetime = Field(description="When it happened (UTC)")
    ip_address: str | None = Field(
        description="The caller's address; only for people who hold `audit_log.read`"
    )
    target_type: str | None = Field(description="The kind of record it is about (`client` ...)")
    target_id: uuid.UUID | None = Field(description="The record it is about")
    changes: list[AuditChange] = Field(description="What the action changed; may be empty")
    summary: str = Field(description="The label with the record it is about")

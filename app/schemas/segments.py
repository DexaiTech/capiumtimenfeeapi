"""Request and response models of client segments and the staff who look after them."""

import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.models import Segment
from app.schemas.common import RequestModel
from app.schemas.settings import blank_is_null, reject_nulls

SegmentName = Annotated[str, Field(min_length=1, max_length=40)]
SegmentDescription = Annotated[str, Field(max_length=500)]


class SegmentRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    is_active: bool
    sort_order: int
    # Live clients with this segment (matched ignoring case) and staff who have not left.
    client_count: int
    staff_count: int
    # False once clients use the segment: DELETE then deactivates it instead.
    can_delete: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, segment: Segment, *, client_count: int, staff_count: int) -> "SegmentRead":
        return cls(
            id=segment.id,
            name=segment.name,
            description=segment.description,
            is_active=segment.is_active,
            sort_order=segment.sort_order,
            client_count=client_count,
            staff_count=staff_count,
            can_delete=client_count == 0,
            created_at=segment.created_at,
            updated_at=segment.updated_at,
        )


class SegmentCreate(RequestModel):
    name: SegmentName
    description: SegmentDescription | None = None
    sort_order: int = Field(default=0, ge=0, le=10_000)
    is_active: bool = True

    _description = field_validator("description")(blank_is_null)


class SegmentUpdate(RequestModel):
    """Every field optional; `description` accepts null to clear it."""

    name: SegmentName | None = None
    description: SegmentDescription | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10_000)
    is_active: bool | None = None

    _description = field_validator("description")(blank_is_null)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, "name", "sort_order", "is_active")
        return self


class StaffSegmentsRead(BaseModel):
    segments: list[SegmentRead] = Field(description="The segments the staff member looks after")
    client_count: int = Field(
        description="Live clients in any of those segments (each counted once)"
    )


class StaffSegmentsWrite(RequestModel):
    segment_ids: list[uuid.UUID] = Field(
        max_length=200, description="Replaces the staff member's segments"
    )

    @field_validator("segment_ids")
    @classmethod
    def _distinct(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(dict.fromkeys(value))

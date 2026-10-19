"""Request and response models of notifications and notification settings."""

import re
import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.notification_types import BY_CODE
from app.db.models import Notification
from app.schemas.common import RequestModel

_NUMBER = re.compile(r"\+?[0-9]{7,15}")


class NotificationRead(BaseModel):
    id: uuid.UUID
    type: str = Field(description="A code of the notification catalogue, e.g. `budget_alert`")
    title: str
    body: str
    link: str | None = Field(
        description="A path inside the practice's app, relative to `/{slug}`: `/time/approvals`"
    )
    entity_type: str | None
    entity_id: uuid.UUID | None
    payload: dict[str, Any]
    is_read: bool
    read_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, row: Notification) -> "NotificationRead":
        return cls(
            id=row.id,
            type=row.type,
            title=row.title,
            body=row.body,
            link=row.link,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            payload=row.payload,
            is_read=row.read_at is not None,
            read_at=row.read_at,
            created_at=row.created_at,
        )


class UnreadCount(BaseModel):
    count: int


class MarkedRead(BaseModel):
    updated: int


def _known_type(value: str) -> str:
    if value not in BY_CODE:
        raise ValueError("Unknown notification type.")
    return value


def normalise_number(value: str | None) -> str | None:
    """Digits with an optional leading +; spaces, dashes, dots and brackets are dropped and a
    blank clears the number."""
    if value is None:
        return None
    stripped = re.sub(r"[\s().-]", "", value)
    if not stripped:
        return None
    if not _NUMBER.fullmatch(stripped):
        raise ValueError("Enter a mobile number of 7 to 15 digits, optionally starting with +.")
    return stripped


class NotificationPreferenceRead(BaseModel):
    type: str
    label: str
    description: str
    enabled: bool = Field(description="False when the practice has switched this type off")
    in_app: bool
    email: bool
    sms: bool
    customised: bool = Field(description="True when the person chose these; else the practice's")


class NotificationSettingsRead(BaseModel):
    sms_number: str | None
    preferences: list[NotificationPreferenceRead]


class PreferenceItem(RequestModel):
    type: str
    in_app: bool
    email: bool
    sms: bool

    _type = field_validator("type")(_known_type)


class NotificationSettingsUpdate(RequestModel):
    """The person's own settings. Types not listed keep what they have. `sms_number` is left
    alone when omitted and cleared by null or a blank."""

    preferences: list[PreferenceItem] = Field(default_factory=list, max_length=100)
    sms_number: str | None = None

    _number = field_validator("sms_number")(normalise_number)

    @model_validator(mode="after")
    def _unique_types(self) -> Self:
        types = [item.type for item in self.preferences]
        if len(types) != len(set(types)):
            raise ValueError("preferences: list each notification type once.")
        return self


class PracticeTypeRead(BaseModel):
    type: str
    label: str
    description: str
    enabled: bool
    in_app: bool = Field(description="Default for people who have not chosen")
    email: bool
    sms: bool


class PracticeNotificationSettingsRead(BaseModel):
    types: list[PracticeTypeRead]


class PracticeTypeItem(RequestModel):
    type: str
    enabled: bool
    in_app: bool
    email: bool
    sms: bool

    _type = field_validator("type")(_known_type)


class PracticeNotificationSettingsUpdate(RequestModel):
    """Types not listed keep what they have."""

    types: list[PracticeTypeItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _unique_types(self) -> Self:
        types = [item.type for item in self.types]
        if len(types) != len(set(types)):
            raise ValueError("types: list each notification type once.")
        return self

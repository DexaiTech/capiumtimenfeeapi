import uuid
from datetime import datetime
from zoneinfo import available_timezones

from pydantic import BaseModel, Field, field_validator

from app.db.models import User, UserSession
from app.schemas.common import RequestModel


class UserRead(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    timezone: str
    email_verified: bool
    must_change_password: bool
    created_at: datetime
    avatar_updated_at: datetime | None = None

    @classmethod
    def from_model(cls, user: User) -> "UserRead":
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            timezone=user.timezone,
            email_verified=user.email_verified,
            must_change_password=user.must_change_password,
            created_at=user.created_at,
            avatar_updated_at=user.avatar_updated_at,
        )


class UserUpdate(RequestModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        if value is not None and value not in available_timezones():
            raise ValueError("Unknown timezone")
        return value


class SessionRead(BaseModel):
    id: uuid.UUID
    ip_address: str | None
    user_agent: str | None
    created_at: datetime
    last_used_at: datetime
    is_current: bool

    @classmethod
    def from_model(cls, session: UserSession, current_id: uuid.UUID) -> "SessionRead":
        return cls(
            id=session.id,
            ip_address=str(session.ip_address) if session.ip_address else None,
            user_agent=session.user_agent,
            created_at=session.created_at,
            last_used_at=session.last_used_at,
            is_current=session.id == current_id,
        )

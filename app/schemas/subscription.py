"""Request and response models of the subscription endpoints."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import RequestModel
from app.services.integrations import SubscriptionStatus
from app.services.subscription import SubscriptionState


class SubscriptionMock(BaseModel):
    available: bool = Field(description="Whether the mock subscription service answers")


class SubscriptionRead(BaseModel):
    status: SubscriptionStatus = Field(description="`active`, `expired` or `suspended`")
    active: bool = Field(description="The practice may use the product (`status` is `active`)")
    reason: str | None = Field(
        description="Why the practice cannot use the product, in words (null while active)"
    )
    checked_at: datetime = Field(description="When the subscription service last answered")
    source: Literal["service", "fallback"] = Field(
        description=(
            "`service`: the subscription service said so (answers are kept for 60 seconds); "
            "`fallback`: it could not be asked, this is the last known (or assumed) status"
        )
    )
    mock: SubscriptionMock | None = Field(
        description="Only while the mock subscription service is in use (never in production)"
    )

    @classmethod
    def build(cls, state: SubscriptionState, mock: SubscriptionMock | None) -> "SubscriptionRead":
        return cls(
            status=state.status,
            active=state.active,
            reason=state.reason,
            checked_at=state.checked_at,
            source=state.source,
            mock=mock,
        )


class SubscriptionMockUpdate(RequestModel):
    status: SubscriptionStatus = Field(
        description="The status the mock reports for this practice from now on"
    )
    reason: str | None = Field(
        default=None,
        max_length=300,
        description="Shown to people who are refused; a default text for the status when omitted",
    )
    available: bool | None = Field(
        default=None,
        description="Switch the whole mock subscription service on or off (an outage)",
    )

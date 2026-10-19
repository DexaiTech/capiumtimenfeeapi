"""The central user service behind one interface.

The Capium group's central directory of users owns a person's name and whether they are active.
Time & Fees applies what happens to a user there (renamed, deactivated, reactivated) to the
members of a practice through :class:`CentralUserGateway`; people are matched by email address.
The real service is one more implementation next to :class:`MockCentralUserGateway`.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from app.services.integrations.common import DEFAULT_CHANGE_BATCH, IntegrationUnavailableError

CentralUserEventType = Literal["renamed", "deactivated", "reactivated"]


@dataclass(frozen=True, slots=True)
class CentralUserEvent:
    """One change at the central user service. ``event_id`` is unique and stable: delivering
    the same event again must change nothing."""

    event_id: str
    type: CentralUserEventType
    central_user_id: str
    email: str
    occurred_at: datetime
    # Only for ``renamed``: the person's new name.
    full_name: str | None = None


@dataclass(frozen=True, slots=True)
class CentralUserChanges:
    """A batch of the change feed. Pass ``cursor`` to the next call to continue after it."""

    events: tuple[CentralUserEvent, ...]
    cursor: str | None
    has_more: bool


class CentralUserGateway(Protocol):
    """A central user service. Every call raises :class:`IntegrationUnavailableError` when the
    service cannot be used."""

    name: str

    async def changes_since(
        self, cursor: str | None, limit: int = DEFAULT_CHANGE_BATCH
    ) -> CentralUserChanges:
        """The events after ``cursor`` (``None``: from the start), oldest first."""
        ...


def central_user_id_for(email: str) -> str:
    """The mock's stable id of a person: ``CU-`` and eight hex digits of their address."""
    digest = hashlib.sha1(email.strip().lower().encode(), usedforsecurity=False)
    return f"CU-{digest.hexdigest()[:8].upper()}"


class MockCentralUserGateway:
    """In-memory change feed for development and tests. State lives in the process."""

    name = "mock"

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Back to an empty change feed, switched on."""
        self._feed: list[tuple[int, CentralUserEvent]] = []
        self._available = True

    @property
    def available(self) -> bool:
        return self._available

    def set_available(self, available: bool) -> None:
        self._available = available

    def push(
        self, event_type: CentralUserEventType, email: str, *, full_name: str | None = None
    ) -> CentralUserEvent:
        """Append an event about the person with this email address to the feed."""
        sequence = len(self._feed) + 1
        event = CentralUserEvent(
            event_id=f"mock-user-evt-{sequence:06d}",
            type=event_type,
            central_user_id=central_user_id_for(email),
            email=email.strip().lower(),
            occurred_at=datetime.now(UTC),
            full_name=full_name,
        )
        self._feed.append((sequence, event))
        return event

    def rename(self, email: str, full_name: str) -> CentralUserEvent:
        return self.push("renamed", email, full_name=full_name)

    def deactivate(self, email: str) -> CentralUserEvent:
        return self.push("deactivated", email)

    def reactivate(self, email: str) -> CentralUserEvent:
        return self.push("reactivated", email)

    async def changes_since(
        self, cursor: str | None, limit: int = DEFAULT_CHANGE_BATCH
    ) -> CentralUserChanges:
        if not self._available:
            raise IntegrationUnavailableError("The mock central user service is switched off.")
        after = int(cursor) if cursor and cursor.isdigit() else 0
        pending = [(seq, event) for seq, event in self._feed if seq > after]
        batch = pending[:limit]
        last = batch[-1][0] if batch else after
        return CentralUserChanges(
            events=tuple(event for _, event in batch),
            cursor=str(last),
            has_more=len(pending) > len(batch),
        )

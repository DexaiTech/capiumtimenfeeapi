"""The central client service behind one interface.

The central service owns the identity of a client of the Capium group: its name, address and
company number, and what happens to it (renamed, archived, restored, merged into another).
Time & Fees talks to it only through :class:`CentralClientGateway`, so the real service is one more
implementation next to :class:`~app.services.central_clients.mock.MockCentralClientGateway`.

Fields of an event that name or address a client use ``None`` for "unchanged" and ``""`` for
"cleared".
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

CentralStatus = Literal["active", "archived"]
CentralEventType = Literal["renamed", "archived", "restored", "merged"]
ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "postcode", "country")
DEFAULT_CHANGE_BATCH = 100


class CentralUnavailableError(Exception):
    """The central service cannot be reached or answered with a failure."""


@dataclass(frozen=True, slots=True)
class CentralClient:
    central_id: str
    name: str
    company_number: str | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    postcode: str | None
    country: str
    status: CentralStatus
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CentralClientEvent:
    """One change at the central service. ``event_id`` is unique and stable: delivering the
    same event again must change nothing."""

    event_id: str
    type: CentralEventType
    central_id: str
    occurred_at: datetime
    name: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    postcode: str | None = None
    country: str | None = None
    # Only for ``merged``: ``central_id`` was merged into this central client.
    into_central_id: str | None = None


@dataclass(frozen=True, slots=True)
class CentralChanges:
    """A batch of the change feed. Pass ``cursor`` to the next call to continue after it."""

    events: tuple[CentralClientEvent, ...]
    cursor: str | None
    has_more: bool


class CentralClientGateway(Protocol):
    """A central client service. Every call raises :class:`CentralUnavailableError` when the
    service cannot be used; the callers then fall back on what they cached."""

    name: str

    async def search(self, q: str, limit: int) -> list[CentralClient]:
        """Clients whose name or company number matches ``q`` (all of them for a blank ``q``),
        by name, at most ``limit``."""
        ...

    async def get(self, central_id: str) -> CentralClient | None:
        """One client, or ``None`` when the central service does not know it."""
        ...

    async def changes_since(
        self, cursor: str | None, limit: int = DEFAULT_CHANGE_BATCH
    ) -> CentralChanges:
        """The events after ``cursor`` (``None``: from the start), oldest first."""
        ...

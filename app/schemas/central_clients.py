"""Request and response models of the central client integration."""

import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.clients import CentralSyncState, ClientDetail
from app.schemas.common import RequestModel

CENTRAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
CentralId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=CENTRAL_ID_PATTERN,
        description="The client's id at the central client service",
    ),
]
CentralStatus = Literal["active", "archived"]
EventType = Literal["renamed", "archived", "restored", "merged"]
EnableResult = Literal["created", "linked_existing", "already_enabled"]
EventOutcome = Literal["applied", "skipped"]

# The fields of a name and address change; "" clears an address field.
_ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "postcode")


# --- Search -----------------------------------------------------------------------------


class MatchingClient(BaseModel):
    id: uuid.UUID = Field(description="The client's id")
    code: str = Field(description="The client's code")
    name: str = Field(description="The client's name")


class CentralClientRead(BaseModel):
    """A central client, as the central service (or this practice's cache of it) describes it."""

    central_id: str = Field(description="The client's id at the central client service")
    name: str = Field(description="The name at the central service")
    company_number: str | None = Field(description="Normalised: upper case, no spaces")
    address_line1: str | None = Field(description="First line of the address")
    address_line2: str | None = Field(description="Second line of the address")
    city: str | None = Field(description="Town or city")
    postcode: str | None = Field(description="Postcode")
    country: str = Field(description="ISO 3166-1 alpha-2 code")
    status: CentralStatus = Field(description="`archived` centrally: it cannot be enabled")
    updated_at: datetime = Field(description="When the central service last changed the client")
    fetched_at: datetime | None = Field(
        description="When this practice's cache last heard of the client (null: not cached yet)"
    )
    enabled: bool = Field(description="A live client of this practice is linked to it")
    client_id: uuid.UUID | None = Field(description="The linked client, when `enabled`")
    client_code: str | None = Field(description="Code of the linked client, when `enabled`")
    matching_client: MatchingClient | None = Field(
        description=(
            "A client of this practice that is not linked and has the same company number: "
            "enabling this central client links it instead of creating a second client"
        )
    )


class CentralSearchResult(BaseModel):
    items: list[CentralClientRead] = Field(description="Matching central clients, by name")
    source: Literal["live", "cache"] = Field(
        description="`cache` when the central service was unavailable"
    )
    stale: bool = Field(description="The items come from the cache and may be out of date")


# --- Enable, link, sync -----------------------------------------------------------------


class CentralEnabled(BaseModel):
    result: EnableResult = Field(
        description=(
            "`created` (a new client), `linked_existing` (a client with the same company "
            "number was linked and took the central name and address) or `already_enabled`"
        )
    )
    from_cache: bool = Field(
        description="The central service was unavailable: the cached copy was used"
    )
    client: ClientDetail = Field(description="The client in full, as GET /clients/{id} returns it")


class CentralLinkRequest(RequestModel):
    central_id: CentralId


class ClientSynced(BaseModel):
    client_id: uuid.UUID = Field(description="The synchronised client")
    code: str = Field(description="The client's code")
    name: str = Field(description="The client's name after the synchronisation")
    central_id: str = Field(description="The central client it is linked to")
    state: CentralSyncState = Field(
        description="`synced`, `stale` (central unavailable: values kept) or `failed`"
    )
    changed: bool = Field(description="The name or an address field changed")
    changes: dict[str, list[str | None]] = Field(
        description="Field -> [old, new] for each field that changed"
    )
    from_cache: bool = Field(description="The central service was unavailable")
    message: str | None = Field(description="What happened, in words")
    synced_at: datetime | None = Field(
        description="When the name and address were last confirmed against the central service"
    )


class CentralSyncAll(BaseModel):
    available: bool = Field(description="The central service answered")
    processed: int = Field(description="Clients handled by this call")
    synced: int = Field(description="Clients now `synced`")
    stale: int = Field(description="Clients now `stale` (the central service was unavailable)")
    failed: int = Field(description="Clients now `failed` (the central service does not know them)")
    changed: int = Field(description="Clients whose name or address changed")
    remaining: int = Field(
        description="Linked clients left for another call (a call handles at most 200)"
    )
    results: list[ClientSynced] = Field(description="One entry per client handled")


# --- Events -----------------------------------------------------------------------------


class _EventFields(RequestModel):
    type: EventType = Field(description="`renamed`, `archived`, `restored` or `merged`")
    central_id: CentralId
    name: str | None = Field(
        default=None, min_length=1, max_length=200, description="The new name (`renamed`)"
    )
    address_line1: str | None = Field(
        default=None, max_length=200, description='New first address line; "" clears it'
    )
    address_line2: str | None = Field(
        default=None, max_length=200, description='New second address line; "" clears it'
    )
    city: str | None = Field(
        default=None, max_length=100, description='New town or city; "" clears it'
    )
    postcode: str | None = Field(
        default=None, max_length=20, description='New postcode; "" clears it'
    )
    country: str | None = Field(
        default=None, pattern=r"^([A-Z]{2})?$", description="New ISO 3166-1 alpha-2 code"
    )
    into_central_id: CentralId | None = Field(
        default=None, description="`merged` only: the central client `central_id` was merged into"
    )

    @field_validator("country", mode="before")
    @classmethod
    def _country_upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.type == "merged":
            if self.into_central_id is None:
                raise ValueError("into_central_id: A merge names the central client it went into")
            if self.into_central_id == self.central_id:
                raise ValueError("into_central_id: A client cannot be merged into itself")
        elif self.into_central_id is not None:
            raise ValueError("into_central_id: Only a merge names a second client")
        if self.type == "renamed" and not any(
            getattr(self, f) is not None for f in ("name", "country", *_ADDRESS_FIELDS)
        ):
            raise ValueError("name: A rename carries a name or an address field")
        return self


class CentralEventRequest(_EventFields):
    """One event from the central client service. Omit a name or address field to leave it
    alone; send an empty string to clear an address field."""

    event_id: str = Field(
        min_length=1,
        max_length=100,
        description="Unique and stable: an event delivered again changes nothing",
    )
    occurred_at: datetime | None = Field(
        default=None, description="When it happened centrally (default: now)"
    )


class MockChangeRequest(_EventFields):
    """A change to push into the mock central service (and its change feed)."""


class CentralEventResult(BaseModel):
    event_id: str = Field(description="The id of the event")
    type: EventType = Field(description="The kind of event")
    central_id: str = Field(description="The central client the event was about")
    outcome: EventOutcome = Field(
        description="`applied`, or `skipped` (unknown or not enabled here, nothing to do, or "
        "the event was delivered before)"
    )
    duplicate: bool = Field(description="The event had been processed before")
    message: str = Field(description="What happened, in words")
    client_id: uuid.UUID | None = Field(description="The client the event changed, if any")


class ProcessedChanges(BaseModel):
    available: bool = Field(description="The central service answered")
    fetched: int = Field(description="Events read from the change feed")
    applied: int = Field(description="Events that changed something")
    skipped: int = Field(description="Events that changed nothing (not enabled here, repeats)")
    failed: int = Field(description="1 when an event failed: the batch stopped, the cursor stayed")
    has_more: bool = Field(description="More events are waiting: call again")
    cursor: str | None = Field(description="Where this practice has read the feed up to")
    events: list[CentralEventResult] = Field(description="The events handled, in order")


# --- The mock ---------------------------------------------------------------------------


class MockAvailability(BaseModel):
    available: bool = Field(description="False: the mock answers as if the service were down")
    gateway: str = Field(description="The gateway in use (`mock`)")


class MockAvailabilityUpdate(RequestModel):
    available: bool = Field(description="Switch the mock central service on or off")


class MockChanged(BaseModel):
    event_id: str = Field(description="The id the mock gave the event")
    type: EventType = Field(description="The kind of event")
    central_id: str = Field(description="The central client it changed")

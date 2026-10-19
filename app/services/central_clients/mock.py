"""The mock central client service: an in-memory catalogue of UK companies and sole traders.

It is deterministic (the same 25 clients with the same unique company numbers every start), can
be switched off to show what the callers do while the central service is down, and keeps a change
feed that tests and demos push renames, archives, restores and merges into. The state lives in
the process: it is for development and demonstrations, never for production.
"""

from dataclasses import replace
from datetime import UTC, datetime

from app.services.central_clients.base import (
    DEFAULT_CHANGE_BATCH,
    CentralChanges,
    CentralClient,
    CentralClientEvent,
    CentralEventType,
    CentralUnavailableError,
)

_CATALOGUE_DATE = datetime(2025, 1, 6, 9, 0, tzinfo=UTC)

# (name, company number, line 1, line 2, city, postcode, status)
_CATALOGUE: tuple[tuple[str, str | None, str, str | None, str, str, str], ...] = (
    ("Northgate Bakery Ltd", "09231457", "12 Northgate Street", None, "Leeds", "LS1 4AB", "active"),
    (
        "Brightwater Plumbing & Heating Ltd",
        "08345129",
        "Unit 4, Riverside Park",
        "Wharf Road",
        "Nottingham",
        "NG2 3HT",
        "active",
    ),
    ("Calder Valley Joinery Ltd", "07765432", "3 Mill Lane", None, "Halifax", "HX1 2PQ", "active"),
    ("Ashby & Partners LLP", "OC412345", "1 Castle Gate", "Suite 2", "Derby", "DE1 3AA", "active"),
    (
        "Kestrel Digital Marketing Ltd",
        "11456789",
        "Floor 3, 20 Deansgate",
        None,
        "Manchester",
        "M3 2BY",
        "active",
    ),
    (
        "Lakeland Outdoor Supplies Ltd",
        "SC654321",
        "8 Rose Street",
        None,
        "Edinburgh",
        "EH2 2PR",
        "active",
    ),
    (
        "Tudor Rose Catering Ltd",
        "10987654",
        "45 High Street",
        None,
        "Cardiff",
        "CF10 1AX",
        "active",
    ),
    (
        "Maple Court Dental Practice Ltd",
        "09876543",
        "2 Maple Court",
        "Station Road",
        "Reading",
        "RG1 1LX",
        "active",
    ),
    (
        "Sandra Whitfield t/a Whitfield Bookkeeping",
        None,
        "27 Orchard Close",
        None,
        "Chelmsford",
        "CM2 6QR",
        "active",
    ),
    (
        "Omar Hassan t/a Hassan Mobile Mechanics",
        None,
        "Unit 12, Canal Yard",
        None,
        "Birmingham",
        "B5 5RP",
        "active",
    ),
    (
        "Pennine Fabrication Ltd",
        "06543210",
        "Bank Mill",
        "Dewsbury Road",
        "Wakefield",
        "WF2 9LN",
        "active",
    ),
    (
        "Greenfield Veterinary Group Ltd",
        "07890123",
        "The Surgery, Church Lane",
        None,
        "Salisbury",
        "SP1 2AB",
        "active",
    ),
    (
        "Harbour Lights Cafe Ltd",
        "NI123456",
        "5 Quay Street",
        None,
        "Belfast",
        "BT1 3JH",
        "active",
    ),
    (
        "Silverline Logistics Ltd",
        "12345670",
        "Depot 6, Gateway Park",
        None,
        "Doncaster",
        "DN4 5TT",
        "active",
    ),
    (
        "Thistle & Oak Interiors Ltd",
        "SC112233",
        "17 George Street",
        None,
        "Aberdeen",
        "AB25 1HZ",
        "active",
    ),
    (
        "Redwood Consulting Partnership",
        None,
        "4 Chapel Row",
        "Bishopsgate",
        "London",
        "EC2N 4AY",
        "active",
    ),
    (
        "Oakridge Care Homes Ltd",
        "08123987",
        "Oakridge House",
        "Ridgeway",
        "Swindon",
        "SN1 4DP",
        "active",
    ),
    ("Penrose Architects Ltd", "05432109", "9 Fore Street", None, "Truro", "TR1 2LH", "active"),
    (
        "Marlowe Property Holdings Ltd",
        "11223344",
        "Marlowe House, 10 King Street",
        None,
        "Bristol",
        "BS1 4EQ",
        "active",
    ),
    (
        "Willow Creek Nurseries Ltd",
        "07001122",
        "Willow Creek Farm",
        "Marsh Lane",
        "Norwich",
        "NR3 1JD",
        "active",
    ),
    ("Dockside Fitness Ltd", "10556677", "2 Dock Road", None, "Liverpool", "L3 4BP", "active"),
    (
        "Hartley Electrical Contractors Ltd",
        "09112233",
        "Unit 9, Trent Business Park",
        None,
        "Stoke-on-Trent",
        "ST4 2QG",
        "active",
    ),
    (
        "Old Mill Printers Ltd",
        "04455667",
        "The Old Mill",
        "Mill Street",
        "Bolton",
        "BL1 2PS",
        "archived",
    ),
    (
        "Fenwick & Wray Solicitors LLP",
        "OC998877",
        "30 Grey Street",
        None,
        "Newcastle upon Tyne",
        "NE1 6AE",
        "active",
    ),
    (
        "Quayside Marine Services Ltd",
        "08667788",
        "Berth 4, Marina Way",
        None,
        "Southampton",
        "SO14 3QH",
        "active",
    ),
)


def _build_catalogue() -> dict[str, CentralClient]:
    clients: dict[str, CentralClient] = {}
    for index, (name, number, line1, line2, city, postcode, status) in enumerate(_CATALOGUE, 1):
        central_id = f"CC-{index:04d}"
        clients[central_id] = CentralClient(
            central_id=central_id,
            name=name,
            company_number=number,
            address_line1=line1,
            address_line2=line2,
            city=city,
            postcode=postcode,
            country="GB",
            status="archived" if status == "archived" else "active",
            updated_at=_CATALOGUE_DATE,
        )
    return clients


def _changed(current: str | None, new: str | None) -> str | None:
    """``None`` leaves the value alone, ``""`` clears it."""
    if new is None:
        return current
    return new or None


def _compact(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum()).upper()


def _applied(client: CentralClient, event: CentralClientEvent) -> CentralClient:
    """``client`` with the name and address fields the event carries."""
    return replace(
        client,
        name=event.name or client.name,
        address_line1=_changed(client.address_line1, event.address_line1),
        address_line2=_changed(client.address_line2, event.address_line2),
        city=_changed(client.city, event.city),
        postcode=_changed(client.postcode, event.postcode),
        country=event.country or client.country,
        updated_at=event.occurred_at,
    )


class MockCentralClientGateway:
    name = "mock"

    def __init__(self) -> None:
        self.reset()

    # --- State for tests and demos ---------------------------------------------------------

    def reset(self) -> None:
        """Back to the catalogue, available, with an empty change feed."""
        self._clients = _build_catalogue()
        self._available = True
        self._feed: list[tuple[int, CentralClientEvent]] = []

    @property
    def available(self) -> bool:
        return self._available

    def set_available(self, available: bool) -> None:
        self._available = available

    def catalogue(self) -> list[CentralClient]:
        """Every client, by central id."""
        return [self._clients[key] for key in sorted(self._clients)]

    def push(
        self,
        event_type: CentralEventType,
        central_id: str,
        *,
        name: str | None = None,
        address_line1: str | None = None,
        address_line2: str | None = None,
        city: str | None = None,
        postcode: str | None = None,
        country: str | None = None,
        into_central_id: str | None = None,
    ) -> CentralClientEvent:
        """Change the catalogue the way the real service would and append the event to the
        feed. Unknown ids raise ``KeyError``."""
        current = self._clients[central_id]
        if event_type == "merged" and (
            into_central_id is None or into_central_id not in self._clients
        ):
            raise KeyError(into_central_id)
        sequence = len(self._feed) + 1
        event = CentralClientEvent(
            event_id=f"mock-evt-{sequence:06d}",
            type=event_type,
            central_id=central_id,
            occurred_at=datetime.now(UTC),
            name=name,
            address_line1=address_line1,
            address_line2=address_line2,
            city=city,
            postcode=postcode,
            country=country,
            into_central_id=into_central_id,
        )
        if event_type == "renamed":
            self._clients[central_id] = _applied(current, event)
        elif event_type in ("archived", "merged"):
            self._clients[central_id] = replace(
                current, status="archived", updated_at=event.occurred_at
            )
        else:
            self._clients[central_id] = replace(
                current, status="active", updated_at=event.occurred_at
            )
        self._feed.append((sequence, event))
        return event

    def rename(self, central_id: str, **fields: str | None) -> CentralClientEvent:
        return self.push("renamed", central_id, **fields)

    def archive(self, central_id: str) -> CentralClientEvent:
        return self.push("archived", central_id)

    def restore(self, central_id: str) -> CentralClientEvent:
        return self.push("restored", central_id)

    def merge(self, central_id: str, into_central_id: str) -> CentralClientEvent:
        return self.push("merged", central_id, into_central_id=into_central_id)

    # --- The gateway interface -------------------------------------------------------------

    def _ensure_available(self) -> None:
        if not self._available:
            raise CentralUnavailableError("The mock central client service is switched off.")

    async def search(self, q: str, limit: int) -> list[CentralClient]:
        self._ensure_available()
        needle = q.strip().lower()
        number = _compact(q) if len(needle) >= 3 else None
        found = [
            client
            for client in self._clients.values()
            if not needle
            or needle in client.name.lower()
            or (
                number is not None
                and client.company_number is not None
                and number in client.company_number
            )
        ]
        found.sort(key=lambda c: (c.name.lower(), c.central_id))
        return found[:limit]

    async def get(self, central_id: str) -> CentralClient | None:
        self._ensure_available()
        return self._clients.get(central_id)

    async def changes_since(
        self, cursor: str | None, limit: int = DEFAULT_CHANGE_BATCH
    ) -> CentralChanges:
        self._ensure_available()
        after = int(cursor) if cursor and cursor.isdigit() else 0
        pending = [(seq, event) for seq, event in self._feed if seq > after]
        batch = pending[:limit]
        last = batch[-1][0] if batch else after
        return CentralChanges(
            events=tuple(event for _, event in batch),
            cursor=str(last),
            has_more=len(pending) > len(batch),
        )

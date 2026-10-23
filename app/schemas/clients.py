"""Request and response models of clients, their contacts, engagements (client services),
the client list, the client overview and the CSV import."""

import calendar
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.db.models import Client, ClientContact, ClientService, Service
from app.schemas.billing import ClientRef
from app.schemas.bulk import MAX_BULK_IDS, need_value, parse_user_id
from app.schemas.common import RequestModel
from app.schemas.rates import ClientRateRead
from app.schemas.settings import Money, blank_is_null, reject_nulls

ClientType = Literal[
    "individual",
    "sole_trader",
    "partnership",
    "llp",
    "limited_company",
    "trust",
    "charity",
    "other",
]
ClientStatus = Literal["prospect", "active", "inactive", "archived"]
AmlStatus = Literal["not_started", "pending", "clear", "review", "failed"]
AccountingSoftware = Literal["xero", "quickbooks", "sage", "freeagent", "other"]
Frequency = Literal["one_off", "monthly", "quarterly", "annually"]
ClientSort = Literal[
    "name", "code", "segment", "health_score", "aml_status", "created_at", "last_activity_at"
]
DeadlineStatus = Literal["overdue", "due_today", "due_soon", "on_track", "none"]
CentralFilter = Literal["linked", "local"]
CentralSyncState = Literal["local", "synced", "stale", "failed"]
# The name and the address of a client that is linked to the central client service are
# managed there: changing them on the client answers 409 CLIENT_FIELD_MANAGED_CENTRALLY.
CENTRALLY_MANAGED_FIELDS = (
    "name",
    "address_line1",
    "address_line2",
    "city",
    "postcode",
    "country",
)

CLIENT_CODE_PATTERN = re.compile(r"^[A-Z0-9_-]{1,20}$")
_NULLABLE_CLIENT_FIELDS = (
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "postcode",
    "company_number",
    "utr",
    "vat_number",
    "billing_notes",
    "notes",
    "segment",
)


@dataclass(frozen=True, slots=True)
class ClientFilters:
    """Filters of the client list and export."""

    q: str | None = None
    # Empty: every status except archived.
    statuses: Sequence[str] = ()
    segments: Sequence[str] = ()
    client_types: Sequence[str] = ()
    aml_statuses: Sequence[str] = ()
    accounting_software: Sequence[str] = ()
    service_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    partner_user_id: uuid.UUID | None = None
    # ``linked``: enabled from the central client service; ``local``: created here.
    central: CentralFilter | None = None


def initials_of(name: str) -> str:
    """Up to two initials for the avatar: "Acme Widgets Ltd" -> "AW", "Zed" -> "ZE",
    "Émile Zola" -> "ÉZ" (words are Unicode letters and digits, not just ASCII)."""
    words = [w for w in re.split(r"[\W_]+", name) if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def normalise_code(value: str | None) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if not code:
        return None
    if not CLIENT_CODE_PATTERN.fullmatch(code):
        raise ValueError("Use 1-20 letters, digits, '_' or '-'")
    return code


def check_year_end(month: int | None, day: int | None) -> None:
    if (month is None) != (day is None):
        raise ValueError("year_end_day: Give both the year-end month and day, or neither")
    if month is not None and day is not None and day > calendar.monthrange(2024, month)[1]:
        raise ValueError("year_end_day: This day does not exist in that month")


# --- Contacts ---------------------------------------------------------------------------


class ContactRead(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    name: str
    position: str | None
    email: str | None
    phone: str | None
    is_primary: bool
    is_billing: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, contact: ClientContact) -> "ContactRead":
        return cls(
            id=contact.id,
            client_id=contact.client_id,
            name=contact.name,
            position=contact.position,
            email=contact.email,
            phone=contact.phone,
            is_primary=contact.is_primary,
            is_billing=contact.is_billing,
            created_at=contact.created_at,
            updated_at=contact.updated_at,
        )


class ContactCreate(RequestModel):
    name: str = Field(min_length=1, max_length=120)
    position: str | None = Field(default=None, max_length=120)
    email: EmailStr | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    # Left out: the first contact of a client becomes its primary contact.
    is_primary: bool | None = None
    is_billing: bool = False

    _blank = field_validator("position", "phone")(blank_is_null)

    @field_validator("email", mode="before")
    @classmethod
    def _blank_email(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


class ContactUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    position: str | None = Field(default=None, max_length=120)
    email: EmailStr | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    is_primary: bool | None = None
    is_billing: bool | None = None

    _blank = field_validator("position", "phone")(blank_is_null)

    @field_validator("email", mode="before")
    @classmethod
    def _blank_email(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, "name", "is_primary", "is_billing")
        return self


# --- Engagements (client services) ------------------------------------------------------


class ClientServiceRead(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    service_id: uuid.UUID
    service_code: str
    service_name: str
    billing_basis: str
    # The agreed fee per occurrence (null: the service's default fee applies).
    fee: int | None
    effective_fee: int | None
    frequency: str
    start_date: date | None
    end_date: date | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, engagement: ClientService, service: Service) -> "ClientServiceRead":
        return cls(
            id=engagement.id,
            client_id=engagement.client_id,
            service_id=engagement.service_id,
            service_code=service.code,
            service_name=service.name,
            billing_basis=service.billing_basis,
            fee=engagement.fee,
            effective_fee=engagement.fee if engagement.fee is not None else service.default_fee,
            frequency=engagement.frequency,
            start_date=engagement.start_date,
            end_date=engagement.end_date,
            is_active=engagement.is_active,
            created_at=engagement.created_at,
            updated_at=engagement.updated_at,
        )


class ClientServiceCreate(RequestModel):
    service_id: uuid.UUID
    fee: Money | None = None
    frequency: Frequency = "annually"
    start_date: date | None = None
    end_date: date | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def _dates(self) -> Self:
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date: The end date is before the start date")
        return self


class ClientServiceUpdate(RequestModel):
    fee: Money | None = None
    frequency: Frequency | None = None
    start_date: date | None = None
    end_date: date | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, "frequency", "is_active")
        return self


# --- Clients ----------------------------------------------------------------------------


class _ClientFields(RequestModel):
    """Validation shared by create and update."""

    @field_validator(*_NULLABLE_CLIENT_FIELDS, check_fields=False)
    @classmethod
    def _blank_is_null(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("email", "invoice_email", mode="before", check_fields=False)
    @classmethod
    def _blank_email(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("company_number", "utr", "vat_number", check_fields=False)
    @classmethod
    def _upper_ref(cls, value: str | None) -> str | None:
        return value.replace(" ", "").upper() or None if value else None

    @field_validator("code", check_fields=False)
    @classmethod
    def _code(cls, value: str | None) -> str | None:
        return normalise_code(value)


class ClientCreate(_ClientFields):
    code: str | None = Field(
        default=None, max_length=20, description="Generated from the client sequence if omitted"
    )
    name: str = Field(min_length=1, max_length=200)
    client_type: ClientType = "limited_company"
    status: ClientStatus = "active"
    email: EmailStr | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    postcode: str | None = Field(default=None, max_length=20)
    country: str = Field(default="GB", pattern=r"^[A-Z]{2}$")
    company_number: str | None = Field(default=None, max_length=20)
    utr: str | None = Field(default=None, max_length=20)
    vat_number: str | None = Field(default=None, max_length=20)
    year_end_month: int | None = Field(default=None, ge=1, le=12)
    year_end_day: int | None = Field(default=None, ge=1, le=31)
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    default_vat_rate_id: uuid.UUID | None = None
    billing_notes: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=5000)
    segment: str | None = Field(default=None, max_length=40)
    aml_status: AmlStatus = "not_started"
    aml_checked_on: date | None = None
    health_score: int | None = Field(default=None, ge=0, le=100)
    accounting_software: AccountingSoftware | None = None
    invoice_email: EmailStr | None = Field(
        default=None,
        max_length=254,
        description="Where invoices are emailed; used before the billing and primary contacts",
    )
    default_service_id: uuid.UUID | None = Field(
        default=None, description="An active service of the practice (404 for any other id)"
    )
    invoice_template_id: uuid.UUID | None = Field(
        default=None, description="An active invoice template of the practice (404 otherwise)"
    )
    contacts: list[ContactCreate] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        check_year_end(self.year_end_month, self.year_end_day)
        if sum(1 for c in self.contacts if c.is_primary) > 1:
            raise ValueError("contacts: Only one contact can be the primary contact")
        return self


class ClientUpdate(_ClientFields):
    code: str | None = Field(default=None, max_length=20)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    client_type: ClientType | None = None
    status: ClientStatus | None = None
    email: EmailStr | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    postcode: str | None = Field(default=None, max_length=20)
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    company_number: str | None = Field(default=None, max_length=20)
    utr: str | None = Field(default=None, max_length=20)
    vat_number: str | None = Field(default=None, max_length=20)
    year_end_month: int | None = Field(default=None, ge=1, le=12)
    year_end_day: int | None = Field(default=None, ge=1, le=31)
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    default_vat_rate_id: uuid.UUID | None = None
    billing_notes: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=5000)
    segment: str | None = Field(default=None, max_length=40)
    aml_status: AmlStatus | None = None
    aml_checked_on: date | None = None
    health_score: int | None = Field(default=None, ge=0, le=100)
    accounting_software: AccountingSoftware | None = None
    invoice_email: EmailStr | None = Field(
        default=None,
        max_length=254,
        description="Where invoices are emailed; null or blank clears it",
    )
    default_service_id: uuid.UUID | None = Field(
        default=None, description="An active service of the practice (404 for any other id)"
    )
    invoice_template_id: uuid.UUID | None = Field(
        default=None, description="An active invoice template of the practice (404 otherwise)"
    )

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, "code", "name", "client_type", "status", "country", "aml_status")
        return self


ClientBulkActionName = Literal["set_status", "set_manager", "set_partner", "set_segment", "delete"]
_CLIENT_STATUSES = ("prospect", "active", "inactive", "archived")


class ClientBulkAction(RequestModel):
    client_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_BULK_IDS)
    action: ClientBulkActionName
    value: str | None = Field(
        default=None,
        description=(
            "`set_status`: a client status (`archived` archives); `set_manager` / "
            "`set_partner`: a staff user id, or null to clear; `set_segment`: a segment of up "
            "to 40 characters, or null/blank to clear; `delete`: no value"
        ),
    )

    @model_validator(mode="after")
    def _value_fits_the_action(self) -> Self:
        value = self.value
        if self.action == "delete":
            if value is not None:
                raise ValueError("value: This action takes no value.")
        elif self.action == "set_status":
            if need_value(value) not in _CLIENT_STATUSES:
                raise ValueError(f"value: Use one of {', '.join(_CLIENT_STATUSES)}.")
        elif self.action == "set_segment":
            if value is not None and len(value) > 40:
                raise ValueError("value: Use at most 40 characters.")
        elif value is not None:
            parse_user_id(value)
        return self

    @property
    def user_id(self) -> uuid.UUID | None:
        return parse_user_id(self.value) if self.value is not None else None

    @property
    def segment(self) -> str | None:
        return self.value or None


class ClientRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    initials: str
    client_type: str
    status: str
    email: str | None
    phone: str | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    postcode: str | None
    country: str
    company_number: str | None
    utr: str | None
    vat_number: str | None
    year_end_month: int | None
    year_end_day: int | None
    partner_user_id: uuid.UUID | None
    manager_user_id: uuid.UUID | None
    payment_terms_days: int | None
    default_vat_rate_id: uuid.UUID | None
    billing_notes: str | None
    notes: str | None
    segment: str | None
    aml_status: str
    aml_checked_on: date | None
    health_score: int | None
    accounting_software: str | None
    invoice_email: str | None = Field(
        description="Where invoices are emailed; used before the billing and primary contacts"
    )
    default_service_id: uuid.UUID | None = Field(
        description="The service new jobs and invoices of the client suggest"
    )
    invoice_template_id: uuid.UUID | None = Field(
        description="The invoice template of the client; null uses the practice default"
    )
    central_client_id: str | None = Field(
        description="Id of the linked central client; null for a client created here"
    )
    central_sync_state: CentralSyncState = Field(
        description=(
            "`local` (not linked), `synced` (name and address confirmed against the central "
            "service), `stale` (the central service was unavailable: cached values) or "
            "`failed` (the central client could not be found)"
        )
    )
    central_synced_at: datetime | None = Field(
        description="When the centrally managed name and address were last confirmed"
    )
    merged_into_client_id: uuid.UUID | None = Field(
        description="Set on a client that was merged away: the client that now holds its work"
    )
    created_at: datetime
    updated_at: datetime

    @classmethod
    def fields_of(cls, client: Client) -> dict[str, object]:
        return {
            "id": client.id,
            "code": client.code,
            "name": client.name,
            "initials": initials_of(client.name),
            "client_type": client.client_type,
            "status": client.status,
            "email": client.email,
            "phone": client.phone,
            "address_line1": client.address_line1,
            "address_line2": client.address_line2,
            "city": client.city,
            "postcode": client.postcode,
            "country": client.country,
            "company_number": client.company_number,
            "utr": client.utr,
            "vat_number": client.vat_number,
            "year_end_month": client.year_end_month,
            "year_end_day": client.year_end_day,
            "partner_user_id": client.partner_user_id,
            "manager_user_id": client.manager_user_id,
            "payment_terms_days": client.payment_terms_days,
            "default_vat_rate_id": client.default_vat_rate_id,
            "billing_notes": client.billing_notes,
            "notes": client.notes,
            "segment": client.segment,
            "aml_status": client.aml_status,
            "aml_checked_on": client.aml_checked_on,
            "health_score": client.health_score,
            "accounting_software": client.accounting_software,
            "invoice_email": client.invoice_email,
            "default_service_id": client.default_service_id,
            "invoice_template_id": client.invoice_template_id,
            "central_client_id": client.central_client_id,
            "central_sync_state": client.central_sync_state,
            "central_synced_at": client.central_synced_at,
            "merged_into_client_id": client.merged_into_client_id,
            "created_at": client.created_at,
            "updated_at": client.updated_at,
        }

    @classmethod
    def from_model(cls, client: Client) -> "ClientRead":
        return cls.model_validate(cls.fields_of(client))


class Deadlines(BaseModel):
    """Derived from the client's open jobs' due dates against today in the practice zone."""

    open_jobs: int = 0
    overdue: int = 0
    # Days the most overdue open job is past its due date (0 when none is overdue).
    overdue_days: int = 0
    due_today: int = 0
    # Due after today and within ``due_soon_days`` days.
    due_soon: int = 0
    due_soon_days: int = 14
    next_due_date: date | None = None
    # The worst state: overdue > due_today > due_soon > on_track (open jobs, none urgent).
    status: DeadlineStatus = "none"


class PrimaryContact(BaseModel):
    id: uuid.UUID
    name: str
    email: str | None
    phone: str | None


class ServiceTag(BaseModel):
    service_id: uuid.UUID
    code: str
    name: str


class TemplateRef(BaseModel):
    id: uuid.UUID
    name: str


class ClientListItem(ClientRead):
    """One row of the client list (all client fields plus the derived columns)."""

    primary_contact: PrimaryContact | None
    # The Contact column (null: the UI offers "+ Add").
    contact_name: str | None
    # Primary contact's email, else the client's own email.
    contact_email: str | None
    deadlines: Deadlines
    # Latest change to the client, its jobs, its time or its invoices.
    last_activity_at: datetime
    # Names of the active engagements, in service display order.
    services: list[ServiceTag]


class Financials(BaseModel):
    """Money is minor units. WIP figures need ``wip.read``, balances ``invoice.read``;
    without the permission they are null."""

    wip_time_value: int | None
    wip_disbursement_value: int | None
    wip_total: int | None
    # Minutes of billable unbilled time that count as WIP.
    unbilled_minutes: int | None
    outstanding_balance: int | None
    overdue_balance: int | None
    # Received but not allocated to any invoice.
    on_account: int | None


class RecurringFees(BaseModel):
    """Active recurring engagements annualised (monthly x12, quarterly x4, annually x1),
    VAT per engagement at the service's (else client default, else practice default) rate,
    rounded half up; 0 VAT when the practice is not VAT registered. One-off fees excluded."""

    annual_net: int
    annual_vat: int
    annual_gross: int
    one_off_net: int


class ClientDetail(ClientListItem):
    contacts: list[ContactRead]
    engagements: list[ClientServiceRead]
    # Client-specific rates; null without ``rate.read``.
    rates: list[ClientRateRead] | None
    financials: Financials
    recurring_fees: RecurringFees
    default_service: ServiceTag | None = Field(
        description="The service named by `default_service_id`"
    )
    invoice_template: TemplateRef | None = Field(
        description="The template named by `invoice_template_id`"
    )
    merged_into: ClientRef | None = Field(description="The client named by `merged_into_client_id`")


class SegmentCount(BaseModel):
    segment: str | None
    count: int


class ClientSummary(BaseModel):
    """Counts for the client list header. ``total`` and the breakdowns other than
    ``by_status`` cover live clients that are not archived (the default list)."""

    total: int
    by_status: dict[str, int]
    by_client_type: dict[str, int]
    by_aml_status: dict[str, int]
    by_accounting_software: dict[str, int]
    by_segment: list[SegmentCount]
    with_overdue_deadlines: int
    by_central: dict[str, int] = Field(
        description="Clients enabled from the central service (`linked`) and created here (`local`)"
    )
    # Active staff members of the practice ("Users 18").
    users: int


# --- Import -----------------------------------------------------------------------------


class ImportRowError(BaseModel):
    row: int = Field(description="1-based data row number (the header is row 0)")
    field: str
    code: str
    message: str


class ImportedClient(BaseModel):
    row: int
    code: str | None
    name: str
    id: uuid.UUID | None = None


class ImportResult(BaseModel):
    dry_run: bool
    total_rows: int
    valid_rows: int
    created: int
    columns: list[str]
    ignored_columns: list[str]
    errors: list[ImportRowError]
    clients: list[ImportedClient]

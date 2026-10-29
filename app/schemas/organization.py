"""Request and response models of the practice, staff (member) and role endpoints."""

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Literal, Self
from zoneinfo import available_timezones

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.db.models import Organization, OrganizationMember, Permission, Role
from app.db.models.organization import FixedFeeWipMode, RateMode
from app.schemas.common import RequestModel
from app.schemas.user import UserRead
from app.utils.reminder_schedule import EARLIEST, LATEST, MAX_STEPS, effective_schedule


class OrganizationRead(BaseModel):
    id: uuid.UUID = Field(description="Id of the practice")
    name: str = Field(description="Practice name")
    slug: str = Field(description="Short URL-safe name, unique across practices; fixed at creation")
    timezone: str = Field(
        description="IANA time zone; calendar dates (time entries, period locks) are in this zone"
    )
    week_start: int = Field(description="First day of the week: 1 (Monday) to 7 (Sunday)")
    working_days: list[int] = Field(description="Working days: 1 (Monday) to 7 (Sunday), sorted")
    minutes_per_day: int = Field(description="Length of a working day in minutes")
    currency: str = Field(
        description="ISO 4217 code; every amount is whole minor units (pence); fixed at creation"
    )
    vat_registered: bool = Field(description="Whether the practice charges VAT")
    vat_number: str | None = Field(description="VAT registration number")
    company_number: str | None = Field(description="Companies House number")
    payment_terms_days: int = Field(description="Default days from issue date to due date")
    default_charge_rate: int = Field(
        description="Pence per hour, the last level of the charge-out rate ladder"
    )
    time_increment_minutes: int = Field(
        description=(
            "Logged time is rounded up to a multiple of this many minutes (1, 5, 6, 10, 15, 30 "
            "or 60); new practices start with 6 (tenths of an hour)"
        )
    )
    require_timesheet_approval: bool = Field(
        description="Whether time must be on an approved timesheet before it can be billed"
    )
    auto_approve_privileged_time: bool = Field(
        description=(
            "Whether the time of people who hold `timesheet.exempt` (Owner, Admin and Partner "
            "by default) counts as approved, and can be billed, without a timesheet approval"
        )
    )
    fixed_fee_wip_mode: FixedFeeWipMode = Field(
        description=(
            "When the unbilled part of a fixed fee counts as WIP: `on_completion` once the job "
            "is completed or closed, `by_budget_progress` in step with the budget hours used"
        )
    )
    wip_adjustment_approval_threshold: int = Field(
        description="Pence: a WIP write-on or write-off above this needs approval (0: all do)"
    )
    wip_write_off_approval_threshold_percent: int | None = Field(
        description=(
            "Basis points (1000 = 10%): a write-off above this share of the job's or client's "
            "current WIP also needs approval, in addition to the pence threshold; null turns "
            "the percentage rule off"
        )
    )
    receipt_required_threshold: int = Field(
        description="Pence (gross): an expense above this must carry a receipt"
    )
    invoice_approval_required: bool = Field(
        description="Whether an invoice at or above the threshold must be approved before issue"
    )
    invoice_approval_threshold: int = Field(
        description="Pence (gross): invoices at or above this need approval when it is on (0: all)"
    )
    payment_reminders_enabled: bool = Field(
        description="Whether overdue invoices are reminded about automatically (daily job)"
    )
    payment_reminder_days_after_due: int = Field(
        description="Days past the due date after which the automatic reminder goes (1-365)"
    )
    payment_reminder_schedule: list[int] = Field(
        description=(
            "The days, counted from an invoice's due date, on which the client is reminded "
            "until the invoice is paid: negative before the due date, 0 on it, positive after. "
            "Without a schedule of its own it is the one day `payment_reminder_days_after_due`."
        )
    )
    mileage_rate: int = Field(description="Pence per mile claimed")
    expense_markup_bp: int = Field(
        description="Basis points added when an expense is recharged to the client (1000 = 10%)"
    )
    rate_mode: RateMode = Field(
        description=(
            "Which rates price time: `staff_first` uses a staff member's own rate before their "
            "grade's, `grade_only` skips the staff-level rate so the grade rate applies"
        )
    )
    overtime_enabled: bool = Field(
        description="Whether overtime is on: time can then be marked as overtime and is priced "
        "at its normal rates times the multiplier"
    )
    overtime_multiplier_bp: int = Field(
        description="Basis points of the normal rate that overtime is paid and charged at "
        "(15000 = 1.5x), unless the staff member has their own"
    )
    email: str | None = Field(description="Practice contact email")
    phone: str | None = Field(description="Practice phone number")
    address_line1: str | None = Field(description="Address line 1")
    address_line2: str | None = Field(description="Address line 2")
    city: str | None = Field(description="Town or city")
    postcode: str | None = Field(description="Postcode")
    country: str = Field(description="ISO 3166 alpha-2 country code")
    bank_details: str | None = Field(description="Bank details printed on invoices")
    invoice_footer: str | None = Field(description="Footer printed on invoices")
    created_at: datetime = Field(description="When the practice was created (UTC)")

    @classmethod
    def from_model(cls, organization: Organization) -> "OrganizationRead":
        return cls(
            id=organization.id,
            name=organization.name,
            slug=organization.slug,
            timezone=organization.timezone,
            week_start=organization.week_start,
            working_days=list(organization.working_days),
            minutes_per_day=organization.minutes_per_day,
            currency=organization.currency,
            vat_registered=organization.vat_registered,
            vat_number=organization.vat_number,
            company_number=organization.company_number,
            payment_terms_days=organization.payment_terms_days,
            default_charge_rate=organization.default_charge_rate,
            time_increment_minutes=organization.time_increment_minutes,
            require_timesheet_approval=organization.require_timesheet_approval,
            auto_approve_privileged_time=organization.auto_approve_privileged_time,
            fixed_fee_wip_mode=organization.fixed_fee_wip_mode,
            wip_adjustment_approval_threshold=organization.wip_adjustment_approval_threshold,
            wip_write_off_approval_threshold_percent=(
                organization.wip_write_off_approval_threshold_percent
            ),
            receipt_required_threshold=organization.receipt_required_threshold,
            invoice_approval_required=organization.invoice_approval_required,
            invoice_approval_threshold=organization.invoice_approval_threshold,
            payment_reminders_enabled=organization.payment_reminders_enabled,
            payment_reminder_days_after_due=organization.payment_reminder_days_after_due,
            payment_reminder_schedule=effective_schedule(
                organization.payment_reminder_schedule, organization.payment_reminder_days_after_due
            ),
            mileage_rate=organization.mileage_rate,
            expense_markup_bp=organization.expense_markup_bp,
            rate_mode=organization.rate_mode,
            overtime_enabled=organization.overtime_enabled,
            overtime_multiplier_bp=organization.overtime_multiplier_bp,
            email=organization.email,
            phone=organization.phone,
            address_line1=organization.address_line1,
            address_line2=organization.address_line2,
            city=organization.city,
            postcode=organization.postcode,
            country=organization.country,
            bank_details=organization.bank_details,
            invoice_footer=organization.invoice_footer,
            created_at=organization.created_at,
        )


class RoleSummary(BaseModel):
    id: uuid.UUID
    name: str
    system_key: str | None

    @classmethod
    def from_model(cls, role: Role) -> "RoleSummary":
        return cls(id=role.id, name=role.name, system_key=role.system_key)


class OrganizationContextRead(BaseModel):
    """A practice together with the caller's role and permissions in it."""

    organization: OrganizationRead
    role: RoleSummary
    permissions: list[str]

    @classmethod
    def build(
        cls,
        organization: Organization,
        role: Role,
        permissions: Iterable[str] | None = None,
    ) -> "OrganizationContextRead":
        """``role.permissions`` must be loaded. ``permissions`` are the person's own (their role's
        with what was granted or denied them); the role's when left out."""
        codes = permissions if permissions is not None else (p.code for p in role.permissions)
        return cls(
            organization=OrganizationRead.from_model(organization),
            role=RoleSummary.from_model(role),
            permissions=sorted(codes),
        )


class MemberRead(BaseModel):
    user_id: uuid.UUID
    full_name: str
    email: str
    role: RoleSummary
    status: str
    manager_user_id: uuid.UUID | None
    weekly_capacity_minutes: int
    chargeable_target_bp: int
    job_title: str | None
    initials: str | None
    must_change_password: bool
    joined_at: datetime
    avatar_updated_at: datetime | None = None

    @classmethod
    def from_model(cls, member: OrganizationMember) -> "MemberRead":
        """``member.user`` and ``member.role`` must be loaded."""
        user = UserRead.from_model(member.user)
        return cls(
            user_id=member.user_id,
            full_name=user.full_name,
            # Staff without a login have a placeholder account: show their work email instead.
            email=user.email if member.login_enabled else (member.work_email or ""),
            role=RoleSummary.from_model(member.role),
            status=member.status,
            manager_user_id=member.manager_user_id,
            weekly_capacity_minutes=member.weekly_capacity_minutes,
            chargeable_target_bp=member.chargeable_target_bp,
            job_title=member.job_title,
            initials=member.initials,
            must_change_password=member.user.must_change_password,
            joined_at=member.joined_at,
            avatar_updated_at=user.avatar_updated_at,
        )


# --- Requests ---------------------------------------------------------------------------

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*[a-z0-9]$"
Money = Annotated[int, Field(ge=0, le=10**12, description="Minor units (pence)")]
# The practice's thresholds (WIP adjustments, receipts, invoice approval): up to £1,000,000.00.
Threshold = Annotated[int, Field(ge=0, le=100_000_000)]


def _known_timezone(value: str | None) -> str | None:
    if value is not None and value not in available_timezones():
        raise ValueError("Unknown timezone")
    return value


def _blank_to_none(value: object) -> object:
    """An empty (or all-blank) string clears a nullable field, the same as ``null``."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _reject_nulls(model: BaseModel, *fields: str) -> None:
    """A PATCH field may be left out, but only nullable columns accept an explicit null."""
    for field in fields:
        if field in model.model_fields_set and getattr(model, field) is None:
            raise ValueError(f"{field}: This field cannot be null")


class OrganizationCreate(RequestModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, min_length=3, max_length=40, pattern=SLUG_PATTERN)
    timezone: str | None = Field(default=None, max_length=64)
    currency: str = Field(default="GBP", pattern=r"^[A-Z]{3}$")
    vat_registered: bool = True

    _timezone = field_validator("timezone")(_known_timezone)


_NULLABLE_SETTINGS = (
    "vat_number",
    "company_number",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "postcode",
    "bank_details",
    "invoice_footer",
    "wip_write_off_approval_threshold_percent",
)


class OrganizationUpdate(RequestModel):
    """Every field is optional; a field that is left out keeps its value."""

    name: str | None = Field(
        default=None, min_length=1, max_length=120, description="Practice name (1-120)"
    )
    timezone: str | None = Field(
        default=None, max_length=64, description="IANA time zone, for example `Europe/London`"
    )
    week_start: int | None = Field(
        default=None, ge=1, le=7, description="First day of the week: 1 (Monday) to 7 (Sunday)"
    )
    working_days: list[int] | None = Field(
        default=None,
        min_length=1,
        max_length=7,
        description="Working days, 1 (Monday) to 7 (Sunday), unique; stored sorted",
    )
    minutes_per_day: int | None = Field(
        default=None, ge=1, le=1440, description="Length of a working day in minutes (1-1440)"
    )
    vat_registered: bool | None = Field(
        default=None, description="Whether the practice charges VAT"
    )
    vat_number: str | None = Field(
        default=None, max_length=20, description="VAT number (up to 20); null or blank clears it"
    )
    company_number: str | None = Field(
        default=None,
        max_length=20,
        description="Companies House number (up to 20); null or blank clears it",
    )
    payment_terms_days: int | None = Field(
        default=None, ge=0, le=365, description="Default days from issue to due date (0-365)"
    )
    default_charge_rate: Money | None = Field(
        default=None, description="Pence per hour, the last level of the charge-out rate ladder"
    )
    time_increment_minutes: Literal[1, 5, 6, 10, 15, 30, 60] | None = Field(
        default=None,
        description="Logged time is rounded up to a multiple of this many minutes",
    )
    require_timesheet_approval: bool | None = Field(
        default=None, description="Whether time must be approved before it can be billed"
    )
    auto_approve_privileged_time: bool | None = Field(
        default=None,
        description=(
            "Whether time of people with `timesheet.exempt` needs no timesheet approval. Applies "
            "to the sheets of those people straight away"
        ),
    )
    fixed_fee_wip_mode: FixedFeeWipMode | None = Field(
        default=None,
        description=(
            "`on_completion`: the unbilled fixed fee is WIP once the job is completed or closed; "
            "`by_budget_progress`: fixed fee x min(1, logged minutes / budget minutes) minus what "
            "is already billed"
        ),
    )
    wip_adjustment_approval_threshold: Threshold | None = Field(
        default=None,
        description=(
            "Pence, 0 to 100000000: a WIP write-on or write-off above this needs approval "
            "(0: every adjustment does)"
        ),
    )
    wip_write_off_approval_threshold_percent: Annotated[int, Field(ge=0, le=10000)] | None = Field(
        default=None,
        description=(
            "Basis points (0-10000; 1000 = 10%): a write-off above this share of the job's or "
            "client's current WIP also needs approval, in addition to the pence threshold; "
            "null turns the percentage rule off"
        ),
    )
    receipt_required_threshold: Threshold | None = Field(
        default=None,
        description="Pence (gross), 0 to 100000000: an expense above this must carry a receipt",
    )
    invoice_approval_required: bool | None = Field(
        default=None,
        description="Whether invoices at or above the threshold need approval to issue",
    )
    invoice_approval_threshold: Threshold | None = Field(
        default=None,
        description=(
            "Pence (gross), 0 to 100000000: with approval on, invoices at or above this need it "
            "(0: every invoice)"
        ),
    )
    payment_reminders_enabled: bool | None = Field(
        default=None, description="Whether overdue invoices are reminded about automatically"
    )
    payment_reminder_days_after_due: int | None = Field(
        default=None,
        ge=1,
        le=365,
        description="Days past the due date after which the automatic reminder goes (1-365)",
    )
    payment_reminder_schedule: list[int] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_STEPS,
        description=(
            f"Reminder days from the due date: up to {MAX_STEPS}, each from {EARLIEST} (before) "
            f"to {LATEST} (after), 0 on the due date. Replaces the schedule."
        ),
    )
    mileage_rate: int | None = Field(
        default=None, ge=0, le=1000, description="Pence per mile claimed (0-1000)"
    )
    expense_markup_bp: int | None = Field(
        default=None,
        ge=0,
        le=100000,
        description="Basis points (0-100000) added when an expense is recharged; 1000 = 10%",
    )
    rate_mode: RateMode | None = Field(
        default=None,
        description=(
            "`staff_first`: a staff member's own rate comes before their grade's; `grade_only`: "
            "the staff-level rate is skipped and the grade rate applies"
        ),
    )
    overtime_enabled: bool | None = Field(
        default=None,
        description=(
            "Whether overtime is on. Off hides it everywhere; time already marked as overtime "
            "keeps its value, and a new or re-rated entry is priced at its normal rate"
        ),
    )
    overtime_multiplier_bp: int | None = Field(
        default=None,
        ge=10000,
        le=100000,
        description="Overtime rate as basis points of the normal rate (10000-100000; 15000 = 1.5x)",
    )
    email: EmailStr | None = Field(
        default=None, max_length=254, description="Contact email; null or blank clears it"
    )
    phone: str | None = Field(
        default=None, max_length=40, description="Phone number (up to 40); null or blank clears it"
    )
    address_line1: str | None = Field(
        default=None, max_length=200, description="Address line 1 (up to 200); null clears it"
    )
    address_line2: str | None = Field(
        default=None, max_length=200, description="Address line 2 (up to 200); null clears it"
    )
    city: str | None = Field(
        default=None, max_length=100, description="Town or city (up to 100); null clears it"
    )
    postcode: str | None = Field(
        default=None, max_length=20, description="Postcode (up to 20); null clears it"
    )
    country: str | None = Field(
        default=None, pattern=r"^[A-Z]{2}$", description="ISO 3166 alpha-2 code, upper case"
    )
    bank_details: str | None = Field(
        default=None, max_length=500, description="Bank details printed on invoices; null clears"
    )
    invoice_footer: str | None = Field(
        default=None, max_length=1000, description="Footer printed on invoices; null clears it"
    )

    _timezone = field_validator("timezone")(_known_timezone)

    _blank = field_validator(*_NULLABLE_SETTINGS, mode="before")(_blank_to_none)

    @field_validator("payment_reminder_schedule")
    @classmethod
    def _reminder_days(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(not EARLIEST <= day <= LATEST for day in value):
            raise ValueError(f"Each day is from {EARLIEST} to {LATEST}.")
        if len(set(value)) != len(value):
            raise ValueError("Each day can appear only once.")
        return sorted(value)

    @field_validator("working_days")
    @classmethod
    def _valid_days(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        if any(day < 1 or day > 7 for day in value):
            raise ValueError("Days are numbered 1 (Monday) to 7 (Sunday)")
        if len(set(value)) != len(value):
            raise ValueError("Days must be unique")
        return sorted(value)

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        _reject_nulls(self, *(f for f in type(self).model_fields if f not in _NULLABLE_SETTINGS))
        return self


class OrganizationDelete(RequestModel):
    confirm_name: str = Field(min_length=1, max_length=120)


MemberStatus = Literal["active", "suspended", "removed", "inactive", "left"]
Initials = Annotated[str, Field(min_length=1, max_length=6, pattern=r"^[A-Za-z0-9]+$")]


class MemberUpdate(RequestModel):
    role_id: uuid.UUID | None = None
    status: Literal["active", "suspended"] | None = None
    manager_user_id: uuid.UUID | None = None
    weekly_capacity_minutes: int | None = Field(default=None, ge=0, le=10080)
    chargeable_target_bp: int | None = Field(default=None, ge=0, le=10000)
    job_title: str | None = Field(default=None, max_length=120)
    initials: Initials | None = None

    _blank = field_validator("job_title", "initials", mode="before")(_blank_to_none)

    @field_validator("initials")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        _reject_nulls(self, "role_id", "status", "weekly_capacity_minutes", "chargeable_target_bp")
        return self


PermissionCodes = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=100)]], Field(max_length=200)
]


class RoleCreate(RequestModel):
    name: str = Field(min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=300)
    permissions: PermissionCodes

    @field_validator("description")
    @classmethod
    def _blank_is_null(cls, value: str | None) -> str | None:
        return value or None


class RoleUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=300)

    @field_validator("description")
    @classmethod
    def _blank_is_null(cls, value: str | None) -> str | None:
        return value or None

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        _reject_nulls(self, "name")
        return self


class RolePermissionsUpdate(RequestModel):
    permissions: PermissionCodes


class RoleRead(BaseModel):
    id: uuid.UUID
    name: str
    system_key: str | None
    description: str | None
    is_system: bool
    permissions: list[str]
    member_count: int

    @classmethod
    def from_model(cls, role: Role, member_count: int) -> "RoleRead":
        """``role.permissions`` must be loaded."""
        return cls(
            id=role.id,
            name=role.name,
            system_key=role.system_key,
            description=role.description,
            is_system=role.is_system,
            permissions=sorted(p.code for p in role.permissions),
            member_count=member_count,
        )


class PermissionRead(BaseModel):
    code: str
    category: str
    description: str

    @classmethod
    def from_model(cls, permission: Permission) -> "PermissionRead":
        return cls(
            code=permission.code, category=permission.category, description=permission.description
        )


class SystemOrganization(BaseModel):
    name: str
    slug: str


class SystemRead(BaseModel):
    """Public facts about the deployment, for the sign-in screen."""

    closed_deployment: bool
    organization: SystemOrganization | None


class PersonCreate(RequestModel):
    full_name: str = Field(min_length=1, max_length=120)
    email: EmailStr = Field(max_length=254)
    role_id: uuid.UUID
    job_title: str | None = Field(default=None, max_length=120)
    initials: Initials | None = None
    manager_user_id: uuid.UUID | None = None
    weekly_capacity_minutes: int = Field(default=2250, ge=0, le=10080)
    # Also email the person a link to choose their own password (the temporary one still works).
    send_email: bool = True

    _blank = field_validator("job_title", "initials", mode="before")(_blank_to_none)

    @field_validator("initials")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else None


class PersonCreated(BaseModel):
    member: MemberRead
    temporary_password: str
    # True when a "set your password" email was queued for the person.
    email_sent: bool = False


class PasswordReset(BaseModel):
    temporary_password: str

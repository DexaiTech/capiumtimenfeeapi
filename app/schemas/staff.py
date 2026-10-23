"""Request and response models of staff: records, lifecycle and login linking."""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.repositories.staff import StaffRow, normalised_status
from app.schemas.common import RequestModel
from app.schemas.organization import Initials, RoleSummary
from app.schemas.rate_rules import RefRead
from app.schemas.settings import Money, reject_nulls
from app.services.charge_rates import ResolvedRate

StaffStatus = Literal["active", "inactive", "left"]
LoginStatus = Literal["none", "invited", "active"]
StaffSort = Literal["name", "code", "email", "grade", "status", "start_date", "leaving_date"]

StaffCode = Annotated[
    str,
    Field(min_length=1, max_length=20, description="Upper-cased; letters, digits, '_' and '-'"),
]
FullName = Annotated[str, Field(min_length=1, max_length=120)]
WorkingMinutes = Annotated[int, Field(ge=1, le=1440, description="Minutes worked on a working day")]
TargetBp = Annotated[int, Field(ge=0, le=10_000, description="Basis points: 7500 = 75%")]
_CODE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _staff_code(value: str | None) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    if not code or not set(code) <= _CODE_CHARS:
        raise ValueError("Use 1-20 letters, digits, '_' or '-'")
    return code


def _working_days(value: list[int] | None) -> list[int] | None:
    if value is None:
        return None
    if any(day < 1 or day > 7 for day in value):
        raise ValueError("Days are numbered 1 (Monday) to 7 (Sunday)")
    if len(set(value)) != len(value):
        raise ValueError("Days must be unique")
    return sorted(value)


def login_status(row: StaffRow) -> LoginStatus:
    member, user = row.member, row.user
    if not member.login_enabled:
        return "none"
    if member.invited_at is not None and user.password_changed_at <= member.invited_at:
        return "invited"
    return "active"


class ApproverRef(BaseModel):
    id: uuid.UUID
    full_name: str


class UsageRead(BaseModel):
    kind: str = Field(description="Table the records live in, e.g. time_entries")
    label: str
    count: int


class StaffRead(BaseModel):
    id: uuid.UUID = Field(description="The staff member's id (also their user id)")
    code: str
    full_name: str
    email: str | None = Field(description="Work email; for staff with a login, usually the same")
    has_login: bool
    login_email: str | None = Field(description="Sign-in address; null without a login")
    login_status: LoginStatus = Field(
        description="none (no login), invited (waiting for the person to choose a password) "
        "or active"
    )
    invited_at: datetime | None
    status: StaffStatus
    is_selectable: bool = Field(
        description="Active and not past the leaving date: time can be recorded for them"
    )
    grade: RefRead | None = Field(description="Null for staff created before grades existed")
    approver: ApproverRef | None = Field(description="Who approves their timesheets")
    role: RoleSummary = Field(description="The role they have (or will have) once they sign in")
    job_title: str | None
    initials: str | None
    working_minutes_per_day: int
    working_days: list[int] = Field(description="ISO weekdays, 1 = Monday")
    weekly_capacity_minutes: int
    chargeable_target_bp: int
    start_date: date | None
    leaving_date: date | None
    # The standard rates that apply to them today (no client, job or service): null without
    # `rate.read`. The source says which level of the rate ladder priced it.
    charge_rate: int | None
    charge_rate_source: str | None
    cost_rate: int | None
    cost_rate_source: str | None
    joined_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(
        cls, row: StaffRow, *, today: date, rate: ResolvedRate | None, show_rates: bool
    ) -> "StaffRead":
        member, user = row.member, row.user
        status = normalised_status(member.status)
        shown = rate if show_rates else None
        return cls(
            id=member.user_id,
            code=member.staff_code,
            full_name=user.full_name,
            email=member.work_email,
            has_login=member.login_enabled,
            login_email=user.email if member.login_enabled else None,
            login_status=login_status(row),
            invited_at=member.invited_at,
            status=cast(StaffStatus, status),
            is_selectable=status == "active"
            and (member.leaving_date is None or member.leaving_date >= today),
            grade=RefRead(id=row.grade.id, code=row.grade.code, name=row.grade.name)
            if row.grade
            else None,
            approver=ApproverRef(id=member.manager_user_id, full_name=row.approver_name)
            if member.manager_user_id and row.approver_name
            else None,
            role=RoleSummary.from_model(row.role),
            job_title=member.job_title,
            initials=member.initials,
            working_minutes_per_day=member.working_minutes_per_day,
            working_days=list(member.working_days),
            weekly_capacity_minutes=member.weekly_capacity_minutes,
            chargeable_target_bp=member.chargeable_target_bp,
            start_date=member.start_date,
            leaving_date=member.leaving_date,
            charge_rate=shown.charge_rate if shown else None,
            charge_rate_source=shown.source if shown else None,
            cost_rate=shown.cost_rate if shown else None,
            cost_rate_source=shown.cost_source if shown else None,
            joined_at=member.joined_at,
            updated_at=member.updated_at,
        )


class StaffDetail(StaffRead):
    usage: list[UsageRead] = Field(
        description="What refers to the person (time, expenses, invoices, ...); empty when none"
    )
    can_delete: bool = Field(description="True when nothing refers to them: DELETE works")


# --- Requests ---------------------------------------------------------------------------


class StaffCreate(RequestModel):
    """A staff member without a login. Add a login afterwards with `invite` or `link-login`."""

    full_name: FullName
    grade_id: uuid.UUID
    start_date: date
    code: StaffCode | None = Field(default=None, description="Default: the next staff number")
    email: EmailStr | None = Field(default=None, max_length=254, description="Work email")
    approver_id: uuid.UUID | None = Field(
        default=None, description="Staff member (with a login) who approves their timesheets"
    )
    job_title: str | None = Field(default=None, max_length=120)
    initials: Initials | None = None
    working_minutes_per_day: WorkingMinutes | None = Field(
        default=None, description="Default: the practice's minutes per day"
    )
    working_days: list[int] | None = Field(
        default=None,
        min_length=1,
        max_length=7,
        description="ISO weekdays, 1 = Monday; default the practice's working days",
    )
    chargeable_target_bp: TargetBp | None = Field(
        default=None, description="Default: the grade's target, else 7500"
    )
    leaving_date: date | None = None
    role_id: uuid.UUID | None = Field(
        default=None, description="Role once they have a login; default the Staff role"
    )
    cost_rate: Money | None = Field(
        default=None,
        description=(
            "Pence per hour: also sets the person's own cost rate from `rate_effective_from`. "
            "Needs `rate.manage` besides `member.invite`."
        ),
    )
    charge_rate: Money | None = Field(
        default=None,
        description=(
            "Pence per hour: also sets the person's own charge-out rate from "
            "`rate_effective_from`. Needs `rate.manage` besides `member.invite`."
        ),
    )
    rate_effective_from: date | None = Field(
        default=None, description="When the rates above start; default `start_date`"
    )

    _code = field_validator("code")(_staff_code)
    _days = field_validator("working_days")(_working_days)
    _blank = field_validator("email", "job_title", "initials", mode="before")(_blank_to_none)

    @field_validator("initials")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @model_validator(mode="after")
    def _dates(self) -> Self:
        if self.leaving_date is not None and self.leaving_date < self.start_date:
            raise ValueError("leaving_date: Must not be before start_date")
        return self


class StaffUpdate(RequestModel):
    """Every field optional. `email`, `approver_id`, `job_title`, `initials`, `start_date` and
    `leaving_date` accept null to clear them. Role and status have their own endpoints."""

    full_name: FullName | None = None
    code: StaffCode | None = None
    email: EmailStr | None = Field(default=None, max_length=254)
    grade_id: uuid.UUID | None = None
    approver_id: uuid.UUID | None = None
    job_title: str | None = Field(default=None, max_length=120)
    initials: Initials | None = None
    working_minutes_per_day: WorkingMinutes | None = None
    working_days: list[int] | None = Field(default=None, min_length=1, max_length=7)
    chargeable_target_bp: TargetBp | None = None
    start_date: date | None = None
    leaving_date: date | None = None

    _code = field_validator("code")(_staff_code)
    _days = field_validator("working_days")(_working_days)
    _blank = field_validator("email", "job_title", "initials", mode="before")(_blank_to_none)

    @field_validator("initials")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @model_validator(mode="after")
    def _valid(self) -> Self:
        reject_nulls(
            self,
            "full_name",
            "code",
            "grade_id",
            "working_minutes_per_day",
            "working_days",
            "chargeable_target_bp",
        )
        if (
            self.start_date is not None
            and self.leaving_date is not None
            and self.leaving_date < self.start_date
        ):
            raise ValueError("leaving_date: Must not be before start_date")
        return self


class StaffDeactivate(RequestModel):
    status: Literal["inactive", "left"] = Field(
        default="inactive",
        description="inactive: paused, can come back. left: no longer works for the practice",
    )
    leaving_date: date | None = Field(
        default=None, description="For `left`: default today; ignored for `inactive`"
    )


StaffBulkActionName = Literal[
    "set_grade", "set_approver", "clear_approver", "deactivate", "reactivate", "delete"
]
MAX_BULK_STAFF = 200


class StaffBulkAction(RequestModel):
    """One action on up to 200 staff members, all or nothing."""

    staff_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_BULK_STAFF)
    action: StaffBulkActionName
    value: str | None = Field(
        default=None,
        description=(
            "`set_grade`: a grade id; `set_approver`: a staff id (someone who can sign in); "
            "`deactivate`: `inactive` (default) or `left`; the others take no value"
        ),
    )
    leaving_date: date | None = Field(
        default=None, description="`deactivate` with `left`: default today"
    )

    @model_validator(mode="after")
    def _value_fits_the_action(self) -> Self:
        if self.action in ("set_grade", "set_approver"):
            try:
                uuid.UUID(self.value or "")
            except ValueError as exc:
                raise ValueError("value: Use the id of a grade or a staff member.") from exc
        elif self.action == "deactivate":
            if self.value not in (None, "inactive", "left"):
                raise ValueError("value: Use inactive or left.")
        elif self.value is not None:
            raise ValueError("value: This action takes no value.")
        return self


class StaffBulkResult(BaseModel):
    action: StaffBulkActionName
    affected: int = Field(description="Distinct staff members the action was applied to")
    changed: int = Field(description="Of those, the people who actually changed")


class StaffLinkLogin(RequestModel):
    """Give a staff member without a login access.

    `create_account`: makes the account (sign-in `email`, default the work email) with a
    temporary password returned once. `existing_user`: the record is merged into the practice
    member who already signs in with `email`: their time, rates and jobs move to that member.
    """

    mode: Literal["create_account", "existing_user"]
    email: EmailStr | None = Field(default=None, max_length=254)
    role_id: uuid.UUID | None = Field(default=None, description="create_account: set the role")
    send_email: bool = Field(
        default=True, description="create_account: email a link to choose a password"
    )

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if self.mode == "existing_user" and self.email is None:
            raise ValueError("email: The address of the person's existing login is required")
        return self


class StaffInvite(RequestModel):
    email: EmailStr | None = Field(
        default=None, max_length=254, description="Sign-in address; default the work email"
    )
    role_id: uuid.UUID | None = Field(default=None, description="Set the role (first invitation)")


class StaffLinked(BaseModel):
    staff: StaffDetail
    # create_account only; shown once.
    temporary_password: str | None
    email_sent: bool
    # existing_user only: the id the merged record had (the staff id is now the login member's).
    merged_from: uuid.UUID | None


class StaffInvited(BaseModel):
    staff: StaffDetail
    invited_at: datetime
    expires_at: datetime
    resent: bool

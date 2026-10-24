"""Bulk staff import from a CSV file (docs/modules/import-export.md).

* The header row is mapped to staff fields by name (case, spaces and aliases such as
  "Staff name", "Hours per day" or "Mon-Fri" days are tolerated); unknown columns are ignored
  and reported.
* Every row is validated like ``POST /staff`` (grade, code, email and initials uniqueness,
  dates, hours) plus an optional ``cost_rate`` / ``charge_rate`` that creates staff-level rate
  rules like ``POST /rates``.
* ``dry_run`` writes nothing. A real run is all-or-nothing: any row error answers 422
  ``IMPORT_INVALID``; otherwise every staff member (without a login, like ``POST /staff``) and
  rate is created in one transaction and one ``staff.imported`` audit entry is written.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, FileUploadError
from app.db.models import Grade, OrganizationMember, User
from app.db.session import unit_of_work
from app.repositories.members import MemberRepository
from app.repositories.rate_rules import Scope
from app.repositories.roles import RoleRepository
from app.repositories.staff import StaffRepository
from app.repositories.users import UserRepository
from app.schemas.clients import ImportRowError
from app.schemas.imports import BulkImportResult, ImportedRecord
from app.schemas.staff import StaffCreate
from app.services import audit, policy, sequences
from app.services.import_csv import (
    CsvRow,
    apply_caster,
    decode_csv,
    extract_csv,
    parse_date,
    parse_int,
    parse_pence,
    parse_percent_bp,
    parse_table,
    pydantic_row_errors,
    row_error,
)
from app.services.import_run import Discard, build_result, invalid_error, write_row
from app.services.members import ensure_initials_free
from app.services.organizations import lock_and_refresh
from app.services.policy import OrgContext
from app.services.rate_rules import RateRuleService, is_exclusion_violation, overlap_error
from app.services.staff_common import (
    new_placeholder_user,
    staff_code_taken_error,
    staff_email_taken_error,
    staff_integrity_error,
)

DEFAULT_TARGET_BP = 7500
_AUDIT_CODES_LIMIT = 100
_CODE_ATTEMPTS = 1000
_CODE_FORMAT = re.compile(r"^[A-Z0-9_-]{1,20}$")

FIELDS = (
    "code",
    "full_name",
    "email",
    "grade",
    "job_title",
    "initials",
    "start_date",
    "leaving_date",
    "working_minutes_per_day",
    "hours_per_day",
    "working_days",
    "chargeable_target_percent",
    "approver_code",
    "cost_rate",
    "charge_rate",
    "rate_effective_from",
)
REQUIRED = ("full_name", "grade", "start_date")
ALIASES = {
    "staff_code": "code",
    "staff_no": "code",
    "staff_number": "code",
    "employee_id": "code",
    "name": "full_name",
    "staff_name": "full_name",
    "staff_member": "full_name",
    "employee": "full_name",
    "work_email": "email",
    "email_address": "email",
    "grade_code": "grade",
    "grade_name": "grade",
    "title": "job_title",
    "position": "job_title",
    "start": "start_date",
    "joined": "start_date",
    "joining_date": "start_date",
    "date_joined": "start_date",
    "leaving": "leaving_date",
    "end_date": "leaving_date",
    "date_left": "leaving_date",
    "minutes_per_day": "working_minutes_per_day",
    "working_hours_per_day": "hours_per_day",
    "days": "working_days",
    "work_days": "working_days",
    "target": "chargeable_target_percent",
    "chargeable_target": "chargeable_target_percent",
    "target_percent": "chargeable_target_percent",
    "chargeable_target_pct": "chargeable_target_percent",
    "approver": "approver_code",
    "approver_staff_code": "approver_code",
    "cost": "cost_rate",
    "cost_rate_per_hour": "cost_rate",
    "charge": "charge_rate",
    "charge_out_rate": "charge_rate",
    "chargeout_rate": "charge_rate",
    "charge_rate_per_hour": "charge_rate",
    "rate_from": "rate_effective_from",
    "rates_effective_from": "rate_effective_from",
    "rate_start_date": "rate_effective_from",
}
_DAY_NAMES = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}


def parse_working_days(value: str) -> list[int]:
    """``Mon-Fri``, ``Mon,Wed,Fri`` or ``1,2,3,4,5`` (1 = Monday) as sorted ISO weekdays."""

    def one(token: str) -> int:
        key = token.strip().lower()[:3]
        if key in _DAY_NAMES:
            return _DAY_NAMES[key]
        if key.isdigit() and 1 <= int(key) <= 7:
            return int(key)
        raise ValueError("Use days like Mon-Fri, Mon;Wed;Fri or 1,2,3,4,5 (1 = Monday).")

    days: set[int] = set()
    for part in re.split(r"[;,/\s]+", value.strip()):
        if not part:
            continue
        if "-" in part:
            first, _, last = part.partition("-")
            start, end = one(first), one(last)
            # Fri-Mon wraps over the weekend.
            days.update(
                range(start, end + 1) if start <= end else [*range(start, 8), *range(1, end + 1)]
            )
        else:
            days.add(one(part))
    if not days:
        raise ValueError("Name at least one working day.")
    return sorted(days)


def format_working_days(days: list[int]) -> str:
    """The inverse of ``parse_working_days``: ``Mon-Fri`` for a run, else ``Mon;Wed;Fri``."""
    names = list(_DAY_NAMES)
    ordered = sorted(days)
    if len(ordered) > 2 and ordered == list(range(ordered[0], ordered[-1] + 1)):
        return f"{names[ordered[0] - 1].title()}-{names[ordered[-1] - 1].title()}"
    return ";".join(names[d - 1].title() for d in ordered)


@dataclass(slots=True)
class _Plan:
    line: int
    data: StaffCreate
    grade: Grade
    approver_id: uuid.UUID | None
    cost: int | None = None
    charge: int | None = None
    rate_from: date | None = None
    errors: list[ImportRowError] = field(default_factory=list)


class StaffImportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.members = MemberRepository(session)
        self.staff = StaffRepository(session)
        self.roles = RoleRepository(session)
        self.users = UserRepository(session)

    async def _grades(self, org_id: uuid.UUID) -> tuple[dict[str, Grade], dict[str, Grade]]:
        grades = (
            await self.session.execute(select(Grade).where(Grade.organization_id == org_id))
        ).scalars()
        by_code: dict[str, Grade] = {}
        by_name: dict[str, Grade] = {}
        for grade in grades:
            by_code[grade.code.upper()] = grade
            by_name[grade.name.lower()] = grade
        return by_code, by_name

    async def _approvers(self, org_id: uuid.UUID) -> dict[str, OrganizationMember]:
        stmt = select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
        )
        found = (await self.session.execute(stmt)).scalars()
        return {m.staff_code.upper(): m for m in found}

    def _plan(
        self,
        row: CsvRow,
        by_code: dict[str, Grade],
        by_name: dict[str, Grade],
        existing: dict[str, OrganizationMember],
        file_codes: set[str],
        errors: list[ImportRowError],
    ) -> _Plan | None:
        line, record = row.line, row.record
        bad: list[ImportRowError] = []

        def cast(field_name: str, fn: Any) -> Any:
            return apply_caster(line, record, field_name, fn, bad)

        for required in REQUIRED:
            if required not in record:
                bad.append(row_error(line, required, "missing", "This value is required."))
        grade: Grade | None = None
        if "grade" in record:
            key = record["grade"]
            grade = by_code.get(key.upper()) or by_name.get(key.lower())
            if grade is None:
                bad.append(row_error(line, "grade", "unknown_grade", f"There is no grade {key}."))
            elif not grade.is_active:
                bad.append(
                    row_error(line, "grade", "inactive_grade", f"The grade {key} is inactive.")
                )
        start = cast("start_date", parse_date)
        leaving = cast("leaving_date", parse_date)
        minutes = cast("working_minutes_per_day", lambda v: parse_int(v, low=1, high=1440))
        hours = cast("hours_per_day", _hours_to_minutes)
        if minutes is not None and hours is not None:
            bad.append(
                row_error(
                    line,
                    "hours_per_day",
                    "duplicate",
                    "Give working_minutes_per_day or hours_per_day, not both.",
                )
            )
        days = cast("working_days", parse_working_days)
        target = cast("chargeable_target_percent", parse_percent_bp)
        cost = cast("cost_rate", parse_pence)
        charge = cast("charge_rate", parse_pence)
        rate_from = cast("rate_effective_from", parse_date)
        approver_id: uuid.UUID | None = None
        if "approver_code" in record:
            code = record["approver_code"].upper()
            approver = existing.get(code)
            if approver is None and code in file_codes:
                bad.append(
                    row_error(
                        line,
                        "approver_code",
                        "approver_cannot_sign_in",
                        f"{code} is created by this file and has no login yet; approvers must "
                        "be able to sign in.",
                    )
                )
            elif approver is None:
                bad.append(
                    row_error(
                        line, "approver_code", "unknown_staff", f"There is no staff member {code}."
                    )
                )
            elif approver.status != "active" or not approver.login_enabled:
                bad.append(
                    row_error(
                        line,
                        "approver_code",
                        "approver_cannot_sign_in",
                        f"{code} must be an active staff member who can sign in.",
                    )
                )
            else:
                approver_id = approver.user_id
        payload: dict[str, Any] = {
            "full_name": record.get("full_name", "x"),
            "grade_id": grade.id if grade else uuid.UUID(int=0),
            # A start date that did not parse already has its error.
            "start_date": start or date(2000, 1, 1),
            "code": record.get("code"),
            "email": record.get("email"),
            "job_title": record.get("job_title"),
            "initials": record.get("initials"),
            "working_minutes_per_day": minutes if minutes is not None else hours,
            "working_days": days,
            "chargeable_target_bp": target,
            "leaving_date": leaving,
        }
        rename = {
            "grade_id": "grade",
            "chargeable_target_bp": "chargeable_target_percent",
        }
        try:
            data = StaffCreate.model_validate(payload)
        except PydanticValidationError as exc:
            seen = {e.field for e in bad}
            for item in pydantic_row_errors(line, exc, rename):
                if item.field not in seen:
                    bad.append(item)
            data = None
        errors.extend(bad)
        if data is None or grade is None or bad:
            return None
        return _Plan(line, data, grade, approver_id, cost, charge, rate_from)

    async def run(
        self,
        ctx: OrgContext,
        content_type: str,
        body: bytes,
        *,
        dry_run: bool,
        rctx: RequestContext,
    ) -> BulkImportResult:
        org_id = ctx.organization_id
        table = parse_table(
            decode_csv(extract_csv(content_type, body)),
            known=FIELDS,
            aliases=ALIASES,
            required=REQUIRED,
        )
        if not table.rows:
            raise FileUploadError("The file has no data rows.")
        errors: list[ImportRowError] = []
        by_code, by_name = await self._grades(org_id)
        existing = await self._approvers(org_id)

        file_codes: set[str] = set()
        seen: dict[str, int] = {}
        for row in table.rows:
            code = row.record.get("code", "").upper()
            if not code:
                continue
            if code in seen:
                errors.append(
                    row_error(
                        row.line,
                        "code",
                        "duplicate",
                        f"The code {code} is also used on row {seen[code]}.",
                    )
                )
            else:
                seen[code] = row.line
            file_codes.add(code)
        duplicated = {e.row for e in errors}
        plans = [
            self._plan(row, by_code, by_name, existing, file_codes, errors) for row in table.rows
        ]
        # A row that repeats an earlier row's code has its error and is not written.
        plans = [
            None if row.line in duplicated else p for row, p in zip(table.rows, plans, strict=True)
        ]

        may_rate = policy.has_permission(ctx, "rate.manage")
        if not may_rate:
            for plan in plans:
                if plan and (plan.cost is not None or plan.charge is not None):
                    errors.append(
                        row_error(
                            plan.line,
                            "cost_rate" if plan.cost is not None else "charge_rate",
                            "permission_denied",
                            "Importing rates needs rate.manage.",
                        )
                    )
                    plan.cost = plan.charge = None

        items: list[ImportedRecord] = []
        rates_created = 0
        try:
            async with unit_of_work(self.session):
                ctx = await lock_and_refresh(self.session, ctx, "member.invite")
                role = await self.roles.get_by_system_key(org_id, "staff")
                if role is None:
                    raise ConflictError("The Staff role is missing.", code="ROLE_MISSING")
                rules = RateRuleService(self.session)
                reserved = file_codes
                for row, plan in zip(table.rows, plans, strict=True):
                    if plan is None:
                        continue
                    outcome = await write_row(
                        self.session,
                        plan.line,
                        "row",
                        errors,
                        partial(self._write, ctx, plan, role.id, reserved, rules, rctx),
                        integrity=_integrity,
                    )
                    if outcome is None:
                        continue
                    user_id, code, rates = outcome
                    rates_created += rates
                    items.append(
                        ImportedRecord(
                            row=row.line,
                            code=code,
                            name=plan.data.full_name,
                            id=None if dry_run else user_id,
                        )
                    )
                if dry_run or errors:
                    raise Discard
                await audit.record(
                    self.session,
                    "staff.imported",
                    ctx=rctx,
                    actor_user_id=ctx.user.id,
                    organization_id=org_id,
                    target_type="member",
                    target_id=None,
                    meta={
                        "count": len(items),
                        "rates": rates_created,
                        "codes": [i.code for i in items[:_AUDIT_CODES_LIMIT]],
                    },
                )
        except Discard:
            pass
        if errors and not dry_run:
            errors.sort(key=lambda e: (e.row, e.field))
            raise invalid_error(errors)
        return build_result(
            table,
            errors,
            items,
            dry_run=dry_run,
            total_rows=len(table.rows),
            created=0 if dry_run else len(items),
            counts={"rates": rates_created},
        )

    async def _next_code(self, org_id: uuid.UUID, reserved: set[str]) -> str:
        for _ in range(_CODE_ATTEMPTS):
            code = (await sequences.next_number(self.session, org_id, "staff")).upper()
            if not _CODE_FORMAT.fullmatch(code):
                raise ConflictError(
                    "The staff numbering produces an invalid code. Fix it in Settings.",
                    code="SEQUENCE_INVALID",
                )
            if code not in reserved and not await self.staff.staff_code_taken(org_id, code):
                return code
        raise ConflictError(
            "No free staff code is left in the numbering sequence. Change it in Settings.",
            code="SEQUENCE_INVALID",
        )

    async def _write(
        self,
        ctx: OrgContext,
        plan: _Plan,
        role_id: uuid.UUID,
        reserved: set[str],
        rules: RateRuleService,
        rctx: RequestContext,
    ) -> tuple[uuid.UUID, str, int]:
        org = ctx.organization
        data = plan.data
        if data.code is not None:
            if await self.staff.staff_code_taken(org.id, data.code):
                raise staff_code_taken_error()
            code = data.code
        else:
            code = await self._next_code(org.id, reserved)
        if data.email and await self.staff.email_taken(org.id, data.email):
            raise staff_email_taken_error()
        await ensure_initials_free(self.members, org.id, data.initials)

        user: User = await self.users.add(await new_placeholder_user(data.full_name))
        minutes = data.working_minutes_per_day or org.minutes_per_day
        days = data.working_days or sorted(org.working_days)
        target = data.chargeable_target_bp
        if target is None:
            target = plan.grade.default_chargeable_target_bp
        if target is None:
            target = DEFAULT_TARGET_BP
        await self.members.add(
            OrganizationMember(
                organization_id=org.id,
                user_id=user.id,
                role_id=role_id,
                status="active",
                manager_user_id=plan.approver_id,
                weekly_capacity_minutes=minutes * len(days),
                chargeable_target_bp=target,
                job_title=data.job_title,
                initials=data.initials,
                staff_code=code,
                work_email=data.email,
                grade_id=plan.grade.id,
                working_minutes_per_day=minutes,
                working_days=days,
                start_date=data.start_date,
                leaving_date=data.leaving_date,
                login_enabled=False,
            )
        )
        rates = 0
        start = plan.rate_from or data.start_date
        for kind, pence in (("cost", plan.cost), ("charge", plan.charge)):
            if pence is None:
                continue
            await rules.write_rule(
                ctx,
                rctx,
                kind=kind,
                scope=Scope(user_id=user.id),
                rate=pence,
                start=start,
                end=None,
                audit_action=None,
                source="import",
            )
            rates += 1
        return user.id, code, rates


def _hours_to_minutes(value: str) -> int:
    try:
        hours = Decimal(value.strip().replace(",", "."))
    except InvalidOperation:
        raise ValueError("Use hours, e.g. 7.5.") from None
    minutes = hours * 60
    if minutes != minutes.to_integral_value() or minutes < 1 or minutes > 1440:
        raise ValueError("Use hours between 0.02 and 24, to the minute (e.g. 7.5).")
    return int(minutes)


def _integrity(exc: Any) -> Any:
    mapped = staff_integrity_error(exc)
    if mapped is None and is_exclusion_violation(exc):
        return overlap_error()
    return mapped

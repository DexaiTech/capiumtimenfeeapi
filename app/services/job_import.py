"""Bulk job import from a CSV file (docs/modules/import-export.md).

* One row = one job, created exactly like ``POST /jobs`` (``JobService.create``: service
  defaults for the billing basis and fee, active client and people, numbering from the ``job``
  sequence, a ``job.created`` audit entry per job).
* ``client_code`` and ``service_code`` name existing records; ``manager``, ``partner`` and the
  ``assignees`` list name staff by staff code or email address.
* ``dry_run`` writes nothing. A real run is all-or-nothing (422 ``IMPORT_INVALID``) and writes one
  ``job.imported`` audit entry in addition to the per-job ones.
"""

import uuid
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import FileUploadError
from app.db.models import Client, OrganizationMember, Service, User
from app.db.session import unit_of_work
from app.schemas.clients import ImportRowError
from app.schemas.imports import BulkImportResult, ImportedRecord
from app.schemas.jobs import JobCreate
from app.services import audit
from app.services.import_csv import (
    CsvRow,
    apply_caster,
    decode_csv,
    enum_value,
    extract_csv,
    parse_date,
    parse_pence,
    parse_table,
    pydantic_row_errors,
    row_error,
    split_list,
)
from app.services.import_run import Discard, build_result, invalid_error, write_row
from app.services.policy import OrgContext
from app.services.time_jobs import JobService

FIELDS = (
    "name",
    "client_code",
    "service_code",
    "status",
    "billing_basis",
    "fixed_fee",
    "hourly_rate",
    "budget_hours",
    "budget_fee",
    "budget_cost",
    "period_start",
    "period_end",
    "start_date",
    "due_date",
    "manager",
    "partner",
    "assignees",
    "description",
)
REQUIRED = ("name", "client_code")
ALIASES = {
    "job_name": "name",
    "job": "name",
    "title": "name",
    "client": "client_code",
    "client_ref": "client_code",
    "service": "service_code",
    "job_status": "status",
    "basis": "billing_basis",
    "billing": "billing_basis",
    "billing_type": "billing_basis",
    "fee": "fixed_fee",
    "fixed_fee_amount": "fixed_fee",
    "rate": "hourly_rate",
    "job_rate": "hourly_rate",
    "budget": "budget_hours",
    "hours_budget": "budget_hours",
    "budget_fee_amount": "budget_fee",
    "expected_cost": "budget_cost",
    "budgeted_cost": "budget_cost",
    "cost_budget": "budget_cost",
    "period_from": "period_start",
    "period_to": "period_end",
    "start": "start_date",
    "due": "due_date",
    "deadline": "due_date",
    "manager_code": "manager",
    "manager_email": "manager",
    "job_manager": "manager",
    "partner_code": "partner",
    "partner_email": "partner",
    "staff": "assignees",
    "assigned_to": "assignees",
    "notes": "description",
}
_RENAME = {
    "client_id": "client_code",
    "service_id": "service_code",
    "manager_user_id": "manager",
    "partner_user_id": "partner",
    "assignee_user_ids": "assignees",
    "budget_minutes": "budget_hours",
}
_AUDIT_LIMIT = 100
_STATUSES = ("planned", "in_progress")


def _hours_to_minutes(value: str) -> int:
    try:
        hours = Decimal(value.strip().replace(",", "."))
    except InvalidOperation:
        raise ValueError("Use hours, e.g. 12.5.") from None
    minutes = hours * 60
    if minutes < 0 or minutes != minutes.to_integral_value() or minutes > 10_000_000:
        raise ValueError("Use a non-negative number of hours, to the minute.")
    return int(minutes)


class JobImportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _people(self, org_id: uuid.UUID) -> dict[str, uuid.UUID]:
        """Staff by upper-cased staff code and lower-cased work or login email."""
        found = await self.session.execute(
            select(
                OrganizationMember.user_id,
                OrganizationMember.staff_code,
                OrganizationMember.work_email,
                OrganizationMember.login_enabled,
                User.email,
            )
            .join(User, User.id == OrganizationMember.user_id)
            .where(OrganizationMember.organization_id == org_id)
        )
        people: dict[str, uuid.UUID] = {}
        for user_id, code, work_email, login, email in found.all():
            if code:
                people[code.upper()] = user_id
            if work_email:
                people.setdefault(work_email.lower(), user_id)
            if login:
                people.setdefault(email.lower(), user_id)
        return people

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
        client_codes = {
            r.record["client_code"].upper() for r in table.rows if "client_code" in r.record
        }
        clients = {
            c.code.upper(): c
            for c in (
                await self.session.execute(
                    select(Client).where(
                        Client.organization_id == org_id,
                        Client.deleted_at.is_(None),
                        func.upper(Client.code).in_(client_codes),
                    )
                )
            ).scalars()
        }
        services = {
            s.code.upper(): s
            for s in (
                await self.session.execute(select(Service).where(Service.organization_id == org_id))
            ).scalars()
        }
        people = await self._people(org_id)
        errors: list[ImportRowError] = []
        plans = [self._plan(r, clients, services, people, errors) for r in table.rows]

        items: list[ImportedRecord] = []
        try:
            async with unit_of_work(self.session):
                jobs = JobService(self.session)
                for row, plan in zip(table.rows, plans, strict=True):
                    if plan is None:
                        continue
                    job = await write_row(
                        self.session,
                        row.line,
                        "row",
                        errors,
                        partial(jobs.create, ctx, plan, rctx),
                        rename=_RENAME,
                    )
                    if job is not None:
                        items.append(
                            ImportedRecord(
                                row=row.line,
                                code=job.number,
                                name=job.name,
                                id=None if dry_run else job.id,
                            )
                        )
                if dry_run or errors:
                    raise Discard
                await audit.record(
                    self.session,
                    "job.imported",
                    ctx=rctx,
                    actor_user_id=ctx.user.id,
                    organization_id=org_id,
                    target_type="job",
                    target_id=None,
                    meta={
                        "count": len(items),
                        "codes": [i.code for i in items[:_AUDIT_LIMIT]],
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
        )

    def _plan(
        self,
        row: CsvRow,
        clients: dict[str, Client],
        services: dict[str, Service],
        people: dict[str, uuid.UUID],
        errors: list[ImportRowError],
    ) -> JobCreate | None:
        line, record = row.line, row.record
        bad: list[ImportRowError] = []
        for required in REQUIRED:
            if required not in record:
                bad.append(row_error(line, required, "missing", "This value is required."))
        client = None
        if "client_code" in record:
            code = record["client_code"].upper()
            client = clients.get(code)
            if client is None:
                bad.append(
                    row_error(line, "client_code", "unknown_client", f"There is no client {code}.")
                )
        service_id = None
        if "service_code" in record:
            code = record["service_code"].upper()
            service = services.get(code)
            if service is None:
                bad.append(
                    row_error(
                        line, "service_code", "unknown_service", f"There is no service {code}."
                    )
                )
            else:
                service_id = service.id

        def person(field: str, value: str) -> uuid.UUID | None:
            key = value.upper() if value.upper() in people else value.lower()
            found = people.get(key)
            if found is None:
                bad.append(
                    row_error(
                        line,
                        field,
                        "unknown_member",
                        f"{value} is not a staff code or email address of this practice.",
                    )
                )
            return found

        manager = person("manager", record["manager"]) if "manager" in record else None
        partner = person("partner", record["partner"]) if "partner" in record else None
        assignees: list[uuid.UUID] = []
        for value in split_list(record.get("assignees", "")):
            found = person("assignees", value)
            if found is not None and found not in assignees:
                assignees.append(found)

        status = None
        if "status" in record:
            status = enum_value(record["status"])
            if status not in _STATUSES:
                bad.append(
                    row_error(
                        line,
                        "status",
                        "invalid",
                        "Use planned or in_progress; later statuses are set on the job.",
                    )
                )
        basis = enum_value(record["billing_basis"]) if "billing_basis" in record else None

        def money(field: str) -> Any:
            return apply_caster(line, record, field, parse_pence, bad)

        def day(field: str) -> Any:
            return apply_caster(line, record, field, parse_date, bad)

        payload: dict[str, Any] = {
            "client_id": client.id if client else uuid.UUID(int=0),
            "name": record.get("name", "x"),
            "description": record.get("description"),
            "service_id": service_id,
            "billing_basis": basis,
            "fixed_fee": money("fixed_fee"),
            "hourly_rate": money("hourly_rate"),
            "budget_minutes": apply_caster(line, record, "budget_hours", _hours_to_minutes, bad),
            "budget_fee": money("budget_fee"),
            "budget_cost": money("budget_cost"),
            "period_start": day("period_start"),
            "period_end": day("period_end"),
            "start_date": day("start_date"),
            "due_date": day("due_date"),
            "manager_user_id": manager,
            "partner_user_id": partner,
            "assignee_user_ids": assignees,
        }
        if status is not None and status in _STATUSES:
            payload["status"] = status
        data = None
        try:
            data = JobCreate.model_validate(payload)
        except PydanticValidationError as exc:
            seen = {e.field for e in bad}
            for item in pydantic_row_errors(line, exc, _RENAME):
                if item.field not in seen:
                    bad.append(item)
        errors.extend(bad)
        return None if bad else data

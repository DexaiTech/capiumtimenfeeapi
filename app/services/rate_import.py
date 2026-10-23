"""Bulk rate-rule import from a CSV file (docs/modules/import-export.md).

* One row = one rate rule (``kind`` cost or charge). Which of ``staff_code``, ``grade``,
  ``client_code`` and ``service_code`` are filled decides the level, exactly as for
  ``POST /rates`` (client + service + staff, client + service, client + staff, client, service +
  grade, staff, grade; cost rates only for staff or grade).
* Rows are written with ``RateRuleService.write_rule``: the same checks, overlap rule and history
  as the UI. ``close_previous`` ends an earlier open-ended rule of the same scope the day before.
* ``dry_run`` writes nothing. A real run is all-or-nothing (422 ``IMPORT_INVALID``) and writes one
  ``rate.imported`` audit entry.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import FileUploadError
from app.db.models import Client, Grade, OrganizationMember, Service
from app.db.session import unit_of_work
from app.repositories.rate_rules import Scope
from app.schemas.clients import ImportRowError
from app.schemas.imports import BulkImportResult, ImportedRecord
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
    pounds,
    row_error,
)
from app.services.import_run import Discard, build_result, invalid_error, write_row
from app.services.policy import OrgContext
from app.services.rate_rules import RateRuleService, is_exclusion_violation, overlap_error

FIELDS = (
    "kind",
    "staff_code",
    "grade",
    "client_code",
    "service_code",
    "rate",
    "effective_from",
    "effective_to",
)
REQUIRED = ("kind", "rate", "effective_from")
ALIASES = {
    "type": "kind",
    "rate_kind": "kind",
    "rate_type": "kind",
    "staff": "staff_code",
    "staff_member": "staff_code",
    "grade_code": "grade",
    "grade_name": "grade",
    "client": "client_code",
    "service": "service_code",
    "amount": "rate",
    "hourly_rate": "rate",
    "rate_per_hour": "rate",
    "from": "effective_from",
    "start_date": "effective_from",
    "valid_from": "effective_from",
    "to": "effective_to",
    "end_date": "effective_to",
    "valid_to": "effective_to",
}
_RENAME = {
    "client_id": "client_code",
    "service_id": "service_code",
    "user_id": "staff_code",
    "grade_id": "grade",
    "scope": "row",
}
_AUDIT_LIMIT = 100


@dataclass(slots=True)
class _Plan:
    line: int
    kind: str
    scope: Scope
    rate: int
    start: date
    end: date | None
    label: str


class RateImportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _lookups(
        self, org_id: uuid.UUID, rows: list[CsvRow]
    ) -> tuple[
        dict[str, uuid.UUID],
        dict[str, Grade],
        dict[str, Grade],
        dict[str, Client],
        dict[str, Service],
    ]:
        staff_codes = {r.record["staff_code"].upper() for r in rows if "staff_code" in r.record}
        client_codes = {r.record["client_code"].upper() for r in rows if "client_code" in r.record}
        service_codes = {
            r.record["service_code"].upper() for r in rows if "service_code" in r.record
        }
        staff: dict[str, uuid.UUID] = {}
        if staff_codes:
            found = await self.session.execute(
                select(OrganizationMember.staff_code, OrganizationMember.user_id).where(
                    OrganizationMember.organization_id == org_id,
                    func.upper(OrganizationMember.staff_code).in_(staff_codes),
                )
            )
            staff = {code.upper(): user_id for code, user_id in found.all()}
        grades = (
            await self.session.execute(select(Grade).where(Grade.organization_id == org_id))
        ).scalars()
        by_code: dict[str, Grade] = {}
        by_name: dict[str, Grade] = {}
        for grade in grades:
            by_code[grade.code.upper()] = grade
            by_name[grade.name.lower()] = grade
        clients: dict[str, Client] = {}
        if client_codes:
            found_clients = await self.session.execute(
                select(Client).where(
                    Client.organization_id == org_id,
                    Client.deleted_at.is_(None),
                    func.upper(Client.code).in_(client_codes),
                )
            )
            clients = {c.code.upper(): c for c in found_clients.scalars()}
        services: dict[str, Service] = {}
        if service_codes:
            found_services = await self.session.execute(
                select(Service).where(
                    Service.organization_id == org_id,
                    func.upper(Service.code).in_(service_codes),
                )
            )
            services = {s.code.upper(): s for s in found_services.scalars()}
        return staff, by_code, by_name, clients, services

    async def run(
        self,
        ctx: OrgContext,
        content_type: str,
        body: bytes,
        *,
        dry_run: bool,
        close_previous: bool,
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
        staff, by_code, by_name, clients, services = await self._lookups(org_id, table.rows)
        errors: list[ImportRowError] = []
        plans: list[_Plan | None] = []
        for row in table.rows:
            plans.append(self._plan(row, staff, by_code, by_name, clients, services, errors))

        items: list[ImportedRecord] = []
        kinds = {"cost": 0, "charge": 0}
        try:
            async with unit_of_work(self.session):
                rules = RateRuleService(self.session)
                for plan in plans:
                    if plan is None:
                        continue
                    rule = await write_row(
                        self.session,
                        plan.line,
                        "effective_from",
                        errors,
                        lambda plan=plan: rules.write_rule(
                            ctx,
                            rctx,
                            kind=plan.kind,
                            scope=plan.scope,
                            rate=plan.rate,
                            start=plan.start,
                            end=plan.end,
                            close_previous=close_previous,
                            audit_action=None,
                            source="import",
                        ),
                        rename=_RENAME,
                        integrity=lambda exc: (
                            overlap_error() if is_exclusion_violation(exc) else None
                        ),
                    )
                    if rule is None:
                        continue
                    kinds[plan.kind] += 1
                    items.append(
                        ImportedRecord(
                            row=plan.line,
                            code=None,
                            name=plan.label,
                            id=None if dry_run else rule.id,
                        )
                    )
                if dry_run or errors:
                    raise Discard
                await audit.record(
                    self.session,
                    "rate.imported",
                    ctx=rctx,
                    actor_user_id=ctx.user.id,
                    organization_id=org_id,
                    target_type="rate_rule",
                    target_id=None,
                    meta={
                        "count": len(items),
                        "cost": kinds["cost"],
                        "charge": kinds["charge"],
                        "close_previous": close_previous,
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
            counts={"cost": kinds["cost"], "charge": kinds["charge"]},
        )

    def _plan(
        self,
        row: CsvRow,
        staff: dict[str, uuid.UUID],
        by_code: dict[str, Grade],
        by_name: dict[str, Grade],
        clients: dict[str, Client],
        services: dict[str, Service],
        errors: list[ImportRowError],
    ) -> _Plan | None:
        line, record = row.line, row.record
        bad: list[ImportRowError] = []
        for required in REQUIRED:
            if required not in record:
                bad.append(row_error(line, required, "missing", "This value is required."))
        kind = enum_value(record["kind"]) if "kind" in record else None
        if kind is not None and kind not in ("cost", "charge"):
            bad.append(row_error(line, "kind", "invalid", "Use cost or charge."))
        rate = apply_caster(line, record, "rate", parse_pence, bad)
        start = apply_caster(line, record, "effective_from", parse_date, bad)
        end = apply_caster(line, record, "effective_to", parse_date, bad)
        if start is not None and end is not None and end < start:
            bad.append(
                row_error(line, "effective_to", "invalid", "Must not be before effective_from.")
            )
        user_id = client_id = service_id = grade_id = None
        parts: list[str] = []
        if "staff_code" in record:
            code = record["staff_code"].upper()
            user_id = staff.get(code)
            parts.append(code)
            if user_id is None:
                bad.append(
                    row_error(
                        line, "staff_code", "unknown_staff", f"There is no staff member {code}."
                    )
                )
        if "grade" in record:
            key = record["grade"]
            grade = by_code.get(key.upper()) or by_name.get(key.lower())
            parts.append(grade.code if grade else key)
            if grade is None:
                bad.append(row_error(line, "grade", "unknown_grade", f"There is no grade {key}."))
            else:
                grade_id = grade.id
        if "client_code" in record:
            code = record["client_code"].upper()
            client = clients.get(code)
            parts.append(code)
            if client is None:
                bad.append(
                    row_error(line, "client_code", "unknown_client", f"There is no client {code}.")
                )
            else:
                client_id = client.id
        if "service_code" in record:
            code = record["service_code"].upper()
            service = services.get(code)
            parts.append(code)
            if service is None:
                bad.append(
                    row_error(
                        line, "service_code", "unknown_service", f"There is no service {code}."
                    )
                )
            else:
                service_id = service.id
        errors.extend(bad)
        if bad or kind is None or rate is None or start is None:
            return None
        who = " / ".join(parts) or "practice"
        label = f"{kind} {pounds(rate)} for {who} from {start.isoformat()}"
        return _Plan(
            line, kind, Scope(client_id, service_id, user_id, grade_id), rate, start, end, label
        )

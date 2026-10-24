"""Bulk client import from a CSV file.

* The header row is mapped to client fields by name (case, spaces and common aliases such
  as "Client name", "Reg No" or "Contact email" are tolerated); unknown columns are
  ignored and reported.
* Every row is validated like ``POST /clients`` plus cross-row checks (duplicate codes in
  the file, codes already used, unknown service codes or staff emails).
* ``dry_run`` returns the per-row errors and writes nothing. A real run is all-or-nothing:
  any error answers 422 ``IMPORT_INVALID`` with the same errors; otherwise every client
  (with its primary contact and engagements) is created in one transaction.
"""

import csv
import io
import re
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import FileUploadError, ValidationError
from app.db.models import Client, ClientContact, ClientService
from app.db.session import unit_of_work
from app.repositories.central_clients import CentralClientRepository
from app.repositories.clients import ClientRepository, ContactRepository, EngagementRepository
from app.repositories.settings import ServiceRepository
from app.schemas.clients import ClientCreate, ImportedClient, ImportResult, ImportRowError
from app.services import audit, policy
from app.services.clients import generate_client_codes
from app.services.import_csv import (
    CSV_CONTENT_TYPES,
    MAX_IMPORT_BYTES,
    MAX_IMPORT_ROWS,
    MAX_UPLOAD_BODY_BYTES,
    ImportTooLargeError,
    UnsupportedUploadError,
    decode_csv,
    extract_csv,
)
from app.services.policy import OrgContext
from app.utils.company_numbers import normalise_company_number
from app.utils.ids import new_id

__all__ = [
    "CSV_CONTENT_TYPES",
    "MAX_IMPORT_BYTES",
    "MAX_IMPORT_ROWS",
    "MAX_UPLOAD_BODY_BYTES",
    "ClientImportService",
    "ImportTooLargeError",
    "UnsupportedUploadError",
    "decode_csv",
    "extract_csv",
]

_AUDIT_CODES_LIMIT = 100
# The name the opening balance import reads the CSV with.
_decode = decode_csv


# Normalised header -> client field. Fields map to themselves too.
_DIRECT_FIELDS = (
    "code",
    "name",
    "client_type",
    "status",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "postcode",
    "country",
    "company_number",
    "utr",
    "vat_number",
    "year_end_month",
    "year_end_day",
    "payment_terms_days",
    "billing_notes",
    "notes",
    "segment",
    "aml_status",
    "aml_checked_on",
    "health_score",
    "accounting_software",
)
_EXTRA_FIELDS = (
    "year_end",
    "contact_name",
    "contact_email",
    "contact_phone",
    "contact_position",
    "services",
    "manager_email",
    "partner_email",
)
_ALIASES = {
    "client": "name",
    "client_name": "name",
    "company_name": "name",
    "client_code": "code",
    "reference": "code",
    "type": "client_type",
    "entity_type": "client_type",
    "client_status": "status",
    "email_address": "email",
    "client_email": "email",
    "telephone": "phone",
    "phone_number": "phone",
    "address": "address_line1",
    "address_1": "address_line1",
    "address_2": "address_line2",
    "town": "city",
    "post_code": "postcode",
    "reg_no": "company_number",
    "registration_number": "company_number",
    "company_no": "company_number",
    "crn": "company_number",
    "vat_no": "vat_number",
    "vat_registration_number": "vat_number",
    "year_end_date": "year_end",
    "payment_terms": "payment_terms_days",
    "aml": "aml_status",
    "health": "health_score",
    "software": "accounting_software",
    "contact": "contact_name",
    "primary_contact": "contact_name",
    "contact_email_address": "contact_email",
    "contact_telephone": "contact_phone",
    "service": "services",
    "service_codes": "services",
    "manager": "manager_email",
    "partner": "partner_email",
}
_ENUM_FIELDS = ("client_type", "status", "aml_status", "accounting_software")
_SPLIT = re.compile(r"[;|,]")
_YEAR_END = re.compile(r"^(\d{1,2})[/.-](\d{1,2})(?:[/.-]\d{2,4})?$")


def _normalise_header(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    key = re.sub(r"^address_line_(\d)$", r"address_line\1", key)
    return _ALIASES.get(key, key)


def _enum_value(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


class _Row:
    __slots__ = ("contact", "data", "line", "manager_email", "partner_email", "service_codes")

    def __init__(self, line: int) -> None:
        self.line = line
        self.data: ClientCreate | None = None
        self.contact: dict[str, Any] | None = None
        self.service_codes: list[str] = []
        self.manager_email: str | None = None
        self.partner_email: str | None = None


class ClientImportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.clients = ClientRepository(session)
        self.central = CentralClientRepository(session)
        self.contacts = ContactRepository(session)
        self.engagements = EngagementRepository(session)
        self.services = ServiceRepository(session)

    def _parse(self, text: str) -> tuple[list[str], list[str], list[_Row], list[ImportRowError]]:
        try:
            reader = csv.reader(io.StringIO(text, newline=""))
            header = next(reader, None)
            if not header or not any(h.strip() for h in header):
                raise FileUploadError("The file has no header row.")
            fields = [_normalise_header(h) for h in header]
            known = set(_DIRECT_FIELDS) | set(_EXTRA_FIELDS)
            used = [f for f in fields if f in known]
            if len(used) != len(set(used)):
                dupes = sorted({f for f in used if used.count(f) > 1})
                raise ValidationError(
                    "Two columns map to the same field.",
                    code="IMPORT_DUPLICATE_COLUMNS",
                    details={"columns": dupes},
                )
            if "name" not in used:
                raise ValidationError(
                    "The file needs a 'name' column.",
                    code="IMPORT_MISSING_COLUMNS",
                    details={"missing": ["name"]},
                )
            ignored = [h for h, f in zip(header, fields, strict=True) if f not in known]
            rows: list[_Row] = []
            errors: list[ImportRowError] = []
            for line, values in enumerate(reader, start=1):
                if not any(v.strip() for v in values):
                    continue
                if len(rows) >= MAX_IMPORT_ROWS:
                    raise ValidationError(
                        f"The file has more than {MAX_IMPORT_ROWS} rows.",
                        code="IMPORT_TOO_MANY_ROWS",
                        details={"max_rows": MAX_IMPORT_ROWS},
                    )
                record = {
                    f: v.strip()
                    for f, v in zip(fields, values, strict=False)
                    if f in known and v.strip()
                }
                rows.append(self._row(line, record, errors))
        except csv.Error as exc:
            raise FileUploadError(f"The CSV could not be read: {exc}.") from exc
        return used, ignored, rows, errors

    def _row(self, line: int, record: dict[str, str], errors: list[ImportRowError]) -> _Row:
        row = _Row(line)
        payload: dict[str, Any] = {k: record[k] for k in _DIRECT_FIELDS if k in record}
        for field in _ENUM_FIELDS:
            if field in payload:
                payload[field] = _enum_value(payload[field])
        if "country" in payload:
            payload["country"] = payload["country"].upper()
        if "year_end" in record:
            match = _YEAR_END.match(record["year_end"])
            if match is None:
                errors.append(
                    ImportRowError(
                        row=line,
                        field="year_end",
                        code="invalid",
                        message="Use DD/MM, e.g. 31/03.",
                    )
                )
            else:
                payload.setdefault("year_end_day", match.group(1))
                payload.setdefault("year_end_month", match.group(2))
        if "contact_name" in record:
            row.contact = {
                "name": record["contact_name"],
                "email": record.get("contact_email"),
                "phone": record.get("contact_phone"),
                "position": record.get("contact_position"),
                "is_primary": True,
            }
            payload["contacts"] = [row.contact]
        elif any(k in record for k in ("contact_email", "contact_phone", "contact_position")):
            errors.append(
                ImportRowError(
                    row=line,
                    field="contact_name",
                    code="missing",
                    message="A contact needs a name.",
                )
            )
        if "services" in record:
            row.service_codes = sorted(
                {c.strip().upper() for c in _SPLIT.split(record["services"]) if c.strip()}
            )
        row.manager_email = record.get("manager_email")
        row.partner_email = record.get("partner_email")
        try:
            row.data = ClientCreate.model_validate(payload)
        except PydanticValidationError as exc:
            for err in exc.errors():
                loc = [str(p) for p in err.get("loc", ())]
                message = str(err.get("msg", "Invalid value.")).removeprefix("Value error, ")
                field = ".".join(loc)
                if not field and ": " in message:
                    candidate, _, rest = message.partition(": ")
                    if candidate.isidentifier():
                        field, message = candidate, rest
                if field.startswith("contacts.0."):
                    field = "contact_" + field.removeprefix("contacts.0.")
                errors.append(
                    ImportRowError(
                        row=line,
                        field=field or "row",
                        code=str(err.get("type", "invalid")),
                        message=message,
                    )
                )
        return row

    async def run(
        self,
        ctx: OrgContext,
        content_type: str,
        body: bytes,
        *,
        dry_run: bool,
        rctx: RequestContext,
    ) -> ImportResult:
        org_id = ctx.organization_id
        columns, ignored, rows, errors = self._parse(decode_csv(extract_csv(content_type, body)))
        if not rows:
            raise FileUploadError("The file has no data rows.")

        # Cross-row and database checks.
        valid = [r for r in rows if r.data is not None]
        seen: dict[str, int] = {}
        for row in valid:
            assert row.data is not None
            code = row.data.code
            if code is None:
                continue
            if code in seen:
                errors.append(
                    ImportRowError(
                        row=row.line,
                        field="code",
                        code="duplicate",
                        message=f"The code {code} is also used on row {seen[code]}.",
                    )
                )
            else:
                seen[code] = row.line
        taken = await self.clients.codes_taken(org_id, set(seen))
        services = await self.services.by_codes(org_id, {c for r in valid for c in r.service_codes})
        emails = {e for r in valid for e in (r.manager_email, r.partner_email) if e}
        members = await self.clients.active_members_by_email(org_id, emails)
        may_archive = policy.has_permission(ctx, "client.delete")
        numbers = {
            n
            for row in valid
            if row.data is not None and (n := normalise_company_number(row.data.company_number))
        }
        enabled = await self.central.linked_with_company_number(org_id, numbers)
        for row in valid:
            assert row.data is not None
            number = normalise_company_number(row.data.company_number)
            existing = enabled.get(number) if number else None
            if existing is not None:
                errors.append(
                    ImportRowError(
                        row=row.line,
                        field="company_number",
                        code="CENTRAL_CLIENT_ALREADY_ENABLED",
                        message=(
                            f"{existing.name} ({existing.code}) is already enabled from the "
                            f"central client service with the company number {number}."
                        ),
                    )
                )
            if row.data.status == "archived" and not may_archive:
                errors.append(
                    ImportRowError(
                        row=row.line,
                        field="status",
                        code="permission_denied",
                        message="Importing archived clients needs client.delete.",
                    )
                )
            if row.data.code in taken:
                errors.append(
                    ImportRowError(
                        row=row.line,
                        field="code",
                        code="CLIENT_CODE_TAKEN",
                        message=f"Another client already uses the code {row.data.code}.",
                    )
                )
            for code in row.service_codes:
                service = services.get(code)
                if service is None or not service.is_active:
                    errors.append(
                        ImportRowError(
                            row=row.line,
                            field="services",
                            code="unknown_service",
                            message=f"There is no active service with the code {code}.",
                        )
                    )
            for field, email in (
                ("manager_email", row.manager_email),
                ("partner_email", row.partner_email),
            ):
                if email and email.lower() not in members:
                    errors.append(
                        ImportRowError(
                            row=row.line,
                            field=field,
                            code="unknown_member",
                            message=f"{email} is not an active member of the practice.",
                        )
                    )
        errors.sort(key=lambda e: (e.row, e.field))
        bad_rows = {e.row for e in errors}
        planned = [
            ImportedClient(row=r.line, code=r.data.code, name=r.data.name)
            for r in rows
            if r.data is not None
        ]
        result = ImportResult(
            dry_run=dry_run,
            total_rows=len(rows),
            valid_rows=sum(1 for r in rows if r.line not in bad_rows),
            created=0,
            columns=columns,
            ignored_columns=ignored,
            errors=errors,
            clients=planned,
        )
        if dry_run:
            return result
        if errors:
            raise ValidationError(
                f"{len(bad_rows)} row(s) have errors; nothing was imported.",
                code="IMPORT_INVALID",
                details=[e.model_dump() for e in errors],
            )

        async with unit_of_work(self.session):
            codes = iter(
                await generate_client_codes(
                    self.session,
                    org_id,
                    sum(1 for r in rows if r.data and not r.data.code),
                    set(seen),
                )
            )
            new_clients: list[Client] = []
            contacts: list[ClientContact] = []
            engagements: list[ClientService] = []
            for row, item in zip(rows, planned, strict=True):
                assert row.data is not None
                values = row.data.model_dump(exclude={"contacts"})
                values["code"] = values["code"] or next(codes)
                if row.manager_email:
                    values["manager_user_id"] = members[row.manager_email.lower()]
                if row.partner_email:
                    values["partner_user_id"] = members[row.partner_email.lower()]
                client_id = new_id()
                new_clients.append(Client(id=client_id, organization_id=org_id, **values))
                item.code, item.id = values["code"], client_id
                for contact in row.data.contacts:
                    contacts.append(
                        ClientContact(
                            organization_id=org_id,
                            client_id=client_id,
                            **contact.model_dump(exclude={"is_primary"}),
                            is_primary=True,
                        )
                    )
                for code in row.service_codes:
                    engagements.append(
                        ClientService(
                            organization_id=org_id,
                            client_id=client_id,
                            service_id=services[code].id,
                        )
                    )
            await self.clients.add_all(new_clients)
            await self.contacts.add_all(contacts)
            await self.engagements.add_all(engagements)
            await audit.record(
                self.session,
                "client.imported",
                ctx=rctx,
                actor_user_id=ctx.user.id,
                organization_id=org_id,
                target_type="client",
                target_id=None,
                meta={
                    "count": len(new_clients),
                    "codes": [c.code for c in new_clients[:_AUDIT_CODES_LIMIT]],
                },
            )
        result.created = len(new_clients)
        result.clients = planned
        return result

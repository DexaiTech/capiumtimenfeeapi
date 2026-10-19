"""Opening-balance time import: historic unbilled time from a client's previous system.

The file's rows (client code, staff email, date, hours, activity code, job number, billable)
are resolved and validated, then handed to ``TimeEntryService.bulk_create`` so that rate
resolution, timesheet linkage, the 24-hour day cap and period locks all use the same,
already-correct logic as an ordinary time entry (`app/services/time_entries.py`). This import
adds only: resolving codes/emails instead of ids, tagging every row created
``is_opening_balance``, and approving a weekly timesheet that the import created outright (and
only that; a week that already had other content is left for the normal approval workflow, so
an import can never silently approve someone else's live, unsubmitted time).

Limits: at most `MAX_OPENING_BALANCE_ROWS` (100) data rows per file, the same cap as a single
bulk time-entry create, since that is what commits the rows. ``dry_run`` validates and writes
nothing; a real run is all-or-nothing, like the client import (`app/services/client_import.py`).
Dates must be ISO ``YYYY-MM-DD`` (not UK ``DD/MM/YYYY``) to avoid ambiguous day/month parsing.
"""

import csv
import io
import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import FileUploadError, ValidationError
from app.db.models import ActivityCode, Client, Job, Organization, TimeEntry, Timesheet
from app.db.session import unit_of_work
from app.repositories.clients import ClientRepository
from app.schemas.opening_balances import (
    MAX_OPENING_BALANCE_ROWS,
    ImportedOpeningBalanceTime,
    OpeningBalanceRowError,
    OpeningBalanceTimeImportResult,
)
from app.schemas.time import TimeEntryBulkCreate, TimeEntryCreate
from app.services.client_import import _decode, extract_csv
from app.services.policy import OrgContext
from app.services.time_entries import TimeEntryService
from app.services.time_rules import week_start_for

_KNOWN_COLUMNS = ("client", "staff", "date", "hours", "activity", "job", "billable")
_REQUIRED_COLUMNS = ("client", "staff", "date", "hours")
_TRUE_TOKENS = {"y", "yes", "true", "1", "billable"}
_FALSE_TOKENS = {"n", "no", "false", "0", "non-billable", "non_billable"}


def _normalise_header(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


@dataclass
class _Row:
    line: int
    client_code: str
    staff_email: str
    entry_date_raw: str
    hours_raw: str
    activity_code: str | None
    job_number: str | None
    billable_raw: str | None


class OpeningBalanceImportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.clients = ClientRepository(session)
        self.entries = TimeEntryService(session)

    def _parse(
        self, text: str
    ) -> tuple[list[str], list[str], list[_Row], list[OpeningBalanceRowError]]:
        try:
            reader = csv.reader(io.StringIO(text, newline=""))
            header = next(reader, None)
            if not header or not any(h.strip() for h in header):
                raise FileUploadError("The file has no header row.")
            fields = [_normalise_header(h) for h in header]
            used = [f for f in fields if f in _KNOWN_COLUMNS]
            missing = [c for c in _REQUIRED_COLUMNS if c not in used]
            if missing:
                raise ValidationError(
                    "The file needs client, staff, date and hours columns.",
                    code="IMPORT_MISSING_COLUMNS",
                    details={"missing": missing},
                )
            ignored = [h for h, f in zip(header, fields, strict=True) if f not in _KNOWN_COLUMNS]
            rows: list[_Row] = []
            errors: list[OpeningBalanceRowError] = []
            for line, values in enumerate(reader, start=1):
                if not any(v.strip() for v in values):
                    continue
                if len(rows) >= MAX_OPENING_BALANCE_ROWS:
                    raise ValidationError(
                        f"The file has more than {MAX_OPENING_BALANCE_ROWS} rows. Split it "
                        "into smaller files.",
                        code="IMPORT_TOO_MANY_ROWS",
                        details={"max_rows": MAX_OPENING_BALANCE_ROWS},
                    )
                record = {f: v.strip() for f, v in zip(fields, values, strict=False) if f in used}
                rows.append(
                    _Row(
                        line=line,
                        client_code=record.get("client", ""),
                        staff_email=record.get("staff", ""),
                        entry_date_raw=record.get("date", ""),
                        hours_raw=record.get("hours", ""),
                        activity_code=record.get("activity") or None,
                        job_number=record.get("job") or None,
                        billable_raw=record.get("billable"),
                    )
                )
        except csv.Error as exc:
            raise FileUploadError(f"The CSV could not be read: {exc}.") from exc
        return used, ignored, rows, errors

    @staticmethod
    def _parse_date(raw: str, line: int, errors: list[OpeningBalanceRowError]) -> date | None:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            errors.append(
                OpeningBalanceRowError(
                    row=line,
                    field="date",
                    code="invalid",
                    message="Use ISO date format YYYY-MM-DD, for example 2025-03-31.",
                )
            )
            return None

    @staticmethod
    def _parse_minutes(raw: str, line: int, errors: list[OpeningBalanceRowError]) -> int | None:
        try:
            hours = float(raw)
        except ValueError:
            errors.append(
                OpeningBalanceRowError(
                    row=line, field="hours", code="invalid", message="Enter hours as a number."
                )
            )
            return None
        minutes = round(hours * 60)
        if minutes < 1 or minutes > 1440:
            errors.append(
                OpeningBalanceRowError(
                    row=line,
                    field="hours",
                    code="invalid",
                    message="Enter hours greater than 0 and at most 24.",
                )
            )
            return None
        return minutes

    @staticmethod
    def _parse_billable(raw: str | None, line: int, errors: list[OpeningBalanceRowError]) -> bool:
        if not raw:
            return True
        token = raw.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        errors.append(
            OpeningBalanceRowError(
                row=line,
                field="billable",
                code="invalid",
                message="Use Y/N (or leave empty for billable).",
            )
        )
        return True

    async def run(
        self,
        ctx: OrgContext,
        content_type: str,
        body: bytes,
        *,
        dry_run: bool,
        rctx: RequestContext,
    ) -> OpeningBalanceTimeImportResult:
        org_id = ctx.organization_id
        columns, ignored, raw_rows, errors = self._parse(_decode(extract_csv(content_type, body)))
        if not raw_rows:
            raise FileUploadError("The file has no data rows.")

        # Resolve codes/emails against the practice's records.
        client_codes = {r.client_code.upper() for r in raw_rows if r.client_code}
        clients_by_code = await self._clients_by_code(org_id, client_codes)
        emails = {r.staff_email for r in raw_rows if r.staff_email}
        members_by_email = await self.clients.active_members_by_email(org_id, emails)
        activity_codes = {r.activity_code.upper() for r in raw_rows if r.activity_code}
        activities_by_code = await self._activities_by_code(org_id, activity_codes)
        job_numbers = {r.job_number.upper() for r in raw_rows if r.job_number}
        jobs_by_number = await self._jobs_by_number(org_id, job_numbers)

        creates: list[TimeEntryCreate] = []
        planned: list[_Row] = []
        touched: list[tuple[uuid.UUID, date]] = []
        for row in raw_rows:
            row_errors_before = len(errors)
            client = clients_by_code.get(row.client_code.upper()) if row.client_code else None
            if row.client_code and client is None:
                errors.append(
                    OpeningBalanceRowError(
                        row=row.line,
                        field="client",
                        code="not_found",
                        message=f"No client with the code {row.client_code}.",
                    )
                )
            user_id = members_by_email.get(row.staff_email.lower()) if row.staff_email else None
            if row.staff_email and user_id is None:
                errors.append(
                    OpeningBalanceRowError(
                        row=row.line,
                        field="staff",
                        code="not_found",
                        message=f"No active staff member with the email {row.staff_email}.",
                    )
                )
            activity = (
                activities_by_code.get(row.activity_code.upper()) if row.activity_code else None
            )
            if row.activity_code and activity is None:
                errors.append(
                    OpeningBalanceRowError(
                        row=row.line,
                        field="activity",
                        code="not_found",
                        message=f"No activity type with the code {row.activity_code}.",
                    )
                )
            job = jobs_by_number.get(row.job_number.upper()) if row.job_number else None
            if row.job_number and job is None:
                errors.append(
                    OpeningBalanceRowError(
                        row=row.line,
                        field="job",
                        code="not_found",
                        message=f"No job numbered {row.job_number}.",
                    )
                )
            if job is not None and client is not None and job.client_id != client.id:
                errors.append(
                    OpeningBalanceRowError(
                        row=row.line,
                        field="job",
                        code="wrong_client",
                        message=f"Job {row.job_number} does not belong to {row.client_code}.",
                    )
                )
            entry_date = self._parse_date(row.entry_date_raw, row.line, errors)
            minutes = self._parse_minutes(row.hours_raw, row.line, errors)
            billable = self._parse_billable(row.billable_raw, row.line, errors)

            if len(errors) > row_errors_before:
                continue
            assert client is not None and user_id is not None
            assert entry_date is not None and minutes is not None
            creates.append(
                TimeEntryCreate(
                    user_id=user_id,
                    entry_date=entry_date,
                    minutes=minutes,
                    client_id=client.id,
                    job_id=job.id if job else None,
                    activity_code_id=activity.id if activity else None,
                    is_billable=billable,
                    description="Imported opening balance",
                )
            )
            planned.append(row)
            touched.append((user_id, entry_date))

        errors.sort(key=lambda e: (e.row, e.field))
        total_rows = len(raw_rows)
        valid_rows = len(creates)
        if dry_run:
            return OpeningBalanceTimeImportResult(
                dry_run=True,
                total_rows=total_rows,
                valid_rows=valid_rows,
                created=0,
                columns=columns,
                ignored_columns=ignored,
                errors=errors,
                entries=[
                    ImportedOpeningBalanceTime(
                        row=row.line,
                        client_code=row.client_code,
                        staff_email=row.staff_email,
                        entry_date=create.entry_date,
                        minutes=create.minutes,
                    )
                    for row, create in zip(planned, creates, strict=True)
                ],
            )
        if errors:
            raise ValidationError(
                f"{len({e.row for e in errors})} row(s) have errors; nothing was imported.",
                code="IMPORT_INVALID",
                details=[e.model_dump() for e in errors],
            )

        async with unit_of_work(self.session):
            views = await self.entries.bulk_create(
                ctx, TimeEntryBulkCreate(entries=creates), rctx
            )
            ids = [v.id for v in views]
            await self._tag_opening_balance(ids)
            await self._approve_fresh_sheets(org_id, set(touched))

        return OpeningBalanceTimeImportResult(
            dry_run=False,
            total_rows=total_rows,
            valid_rows=valid_rows,
            created=len(ids),
            columns=columns,
            ignored_columns=ignored,
            errors=[],
            entries=[
                ImportedOpeningBalanceTime(
                    row=row.line,
                    client_code=row.client_code,
                    staff_email=row.staff_email,
                    entry_date=create.entry_date,
                    minutes=create.minutes,
                    id=entry_id,
                )
                for row, create, entry_id in zip(planned, creates, ids, strict=True)
            ],
        )

    async def _clients_by_code(
        self, organization_id: uuid.UUID, codes: set[str]
    ) -> dict[str, Client]:
        if not codes:
            return {}
        stmt = select(Client).where(
            Client.organization_id == organization_id, Client.code.in_(codes)
        )
        return {c.code: c for c in (await self.session.execute(stmt)).scalars()}

    async def _activities_by_code(
        self, organization_id: uuid.UUID, codes: set[str]
    ) -> dict[str, ActivityCode]:
        if not codes:
            return {}
        stmt = select(ActivityCode).where(
            ActivityCode.organization_id == organization_id, ActivityCode.code.in_(codes)
        )
        return {a.code: a for a in (await self.session.execute(stmt)).scalars()}

    async def _jobs_by_number(
        self, organization_id: uuid.UUID, numbers: set[str]
    ) -> dict[str, Job]:
        if not numbers:
            return {}
        stmt = select(Job).where(Job.organization_id == organization_id, Job.number.in_(numbers))
        return {j.number: j for j in (await self.session.execute(stmt)).scalars()}

    async def _tag_opening_balance(self, entry_ids: list[uuid.UUID]) -> None:
        if not entry_ids:
            return
        await self.session.execute(
            update(TimeEntry).where(TimeEntry.id.in_(entry_ids)).values(is_opening_balance=True)
        )

    async def _approve_fresh_sheets(
        self, organization_id: uuid.UUID, user_dates: set[tuple[uuid.UUID, date]]
    ) -> None:
        """Approve a weekly timesheet only when every entry on it came from this import, so an
        import can never approve a week that already had other, unrelated content on it."""
        organization = await self.session.get(Organization, organization_id)
        assert organization is not None
        weeks = {(user_id, week_start_for(organization, day)) for user_id, day in user_dates}
        for user_id, week_start in weeks:
            sheet = await self.session.scalar(
                select(Timesheet).where(
                    Timesheet.organization_id == organization_id,
                    Timesheet.user_id == user_id,
                    Timesheet.week_start == week_start,
                )
            )
            if sheet is None or sheet.status == "approved":
                continue
            total = await self.session.scalar(
                select(func.count())
                .select_from(TimeEntry)
                .where(TimeEntry.timesheet_id == sheet.id)
            )
            imported = await self.session.scalar(
                select(func.count())
                .select_from(TimeEntry)
                .where(TimeEntry.timesheet_id == sheet.id, TimeEntry.is_opening_balance.is_(True))
            )
            if total == imported:
                sheet.status = "approved"
                sheet.decision_note = "Approved automatically: opening balance import."

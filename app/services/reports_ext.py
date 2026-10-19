"""The expenses, payments and missing-timesheets reports (docs/modules/reports.md).

They share the date rules, the filter checks and the paging of ``ReportService``; their figures
come from the same tables and rules as the screens they summarise (``GET /expenses``,
``GET /payments``, ``GET /timesheets/missing``).
"""

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.repositories.reports import DateRange, ItemFilter
from app.repositories.reports_ext import ExpenseFilter, ReportExtRepository
from app.schemas.reports_ext import (
    MAX_MISSING_WEEKS,
    ExpenseRow,
    ExpensesFilters,
    ExpensesQuery,
    ExpensesReport,
    ExpenseTotals,
    MissingTimesheetRow,
    MissingTimesheetsFilters,
    MissingTimesheetsQuery,
    MissingTimesheetsReport,
    MissingTimesheetsTotals,
    PaymentRow,
    PaymentsFilters,
    PaymentsQuery,
    PaymentsReport,
    PaymentTotals,
)
from app.schemas.time_views import MissingTimesheet
from app.services.expense_access import ExpenseAccess, sees_all
from app.services.policy import OrgContext, ensure_any, has_permission
from app.services.report_paging import window
from app.services.reports import ReportService, bucket_days, months_between
from app.services.time_rules import practice_today, week_start_for
from app.services.timesheet_missing import last_completed_week
from app.services.timesheet_reminders import TimesheetReminderService

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_EXPENSE_FIELDS = (
    "line_count",
    "net",
    "vat",
    "gross",
    "billable_net",
    "recharge_amount",
    "recharged_amount",
    "reimbursable",
)
_PAYMENT_FIELDS = ("payment_count", "received", "allocated", "void_count", "void_amount")
_CLAIM_STATUS_ORDER = ("draft", "submitted", "approved", "returned")
_METHOD_NAMES = {
    "bank_transfer": "Bank transfer",
    "card": "Card",
    "direct_debit": "Direct debit",
    "cash": "Cash",
    "cheque": "Cheque",
    "other": "Other",
}
_NO_EXPENSE_KEY = {"client": "No client (overhead)", "job": "No job"}


def _int(row: RowMapping, key: str) -> int:
    return int(row[key] or 0)


def _sum(rows: Sequence[Any], field: str) -> int:
    return sum(int(getattr(row, field)) for row in rows)


def _month_name(month: date) -> str:
    return f"{_MONTHS[month.month - 1]} {month.year}"


def _month_key(month: date) -> str:
    return f"{month.year:04d}-{month.month:02d}"


class ExtendedReportService(ReportService):
    """``ReportService`` and the reports added to it later."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)
        self.ext = ReportExtRepository(session)
        self.expense_access = ExpenseAccess(session)
        self.timesheets = TimesheetReminderService(session)

    # --- Expenses -----------------------------------------------------------------------

    async def expenses(self, ctx: OrgContext, q: ExpensesQuery) -> ExpensesReport:
        """``report.read`` or ``report.financial``. Whose claims: everyone's with
        ``expense.read_all`` or ``expense.manage``, otherwise one's own and, with
        ``expense.approve``, those of the people one manages (as ``GET /expenses``)."""
        ensure_any(ctx, "report.read", "report.financial")
        period = self._range(ctx, q.date_from, q.date_to)
        await self._check_refs(
            ctx, ItemFilter(client_id=q.client_id, job_id=q.job_id, user_id=q.user_id)
        )
        org_id = ctx.organization_id
        if q.expense_type_id is not None and not await self.ext.expense_type_exists(
            org_id, q.expense_type_id
        ):
            raise NotFoundError("Expense type not found.")
        scope = await self._claimants(ctx)
        if q.user_id is not None and scope is not None and q.user_id not in scope:
            raise AuthorizationError("You can report on your own expenses only.")
        found = await self.ext.expenses(
            org_id,
            period,
            q.group_by,
            ExpenseFilter(
                scope_user_ids=scope,
                claim_statuses=q.status,
                user_id=q.user_id,
                client_id=q.client_id,
                job_id=q.job_id,
                expense_type_id=q.expense_type_id,
                is_billable=q.is_billable,
                billing_statuses=q.billing_status,
            ),
        )
        rows = self._expense_rows(q.group_by, period, found)
        totals = ExpenseTotals(**{field: _sum(rows, field) for field in _EXPENSE_FIELDS})
        filters = ExpensesFilters(
            date_from=period.start,
            date_to=period.end,
            group_by=q.group_by,
            client_id=q.client_id,
            job_id=q.job_id,
            user_id=q.user_id,
            expense_type_id=q.expense_type_id,
            status=list(q.status),
            is_billable=q.is_billable,
            billing_status=list(q.billing_status),
        )
        page_rows, page = window(rows, q)
        return ExpensesReport(rows=page_rows, totals=totals, filters=filters, page=page)

    async def _claimants(self, ctx: OrgContext) -> list[uuid.UUID] | None:
        """Whose claims the caller may report on (``None``: everyone's)."""
        if sees_all(ctx):
            return None
        visible = [ctx.user.id]
        if has_permission(ctx, "expense.approve"):
            visible.extend(await self.expense_access.managed_ids(ctx))
        return visible

    @staticmethod
    def _expense_rows(
        group_by: str, period: DateRange, found: Sequence[RowMapping]
    ) -> list[ExpenseRow]:
        def measures(r: RowMapping | None) -> dict[str, int]:
            return {field: 0 if r is None else _int(r, field) for field in _EXPENSE_FIELDS}

        if group_by == "month":
            assert period.start is not None and period.end is not None
            by_month = {r["key"]: r for r in found}
            return [
                ExpenseRow(
                    key=_month_key(month),
                    code=None,
                    name=_month_name(month),
                    client_name=None,
                    **bucket_days("month", month, period),
                    **measures(by_month.get(month)),
                )
                for month in months_between(period.start, period.end)
            ]
        rows: list[ExpenseRow] = []
        for r in found:
            key = r["key"]
            ids: dict[str, Any] = {}
            match group_by:
                case "type":
                    ids = {"expense_type_id": key}
                case "person":
                    ids = {"user_id": key}
                case "client":
                    ids = {"client_id": key}
                case "job":
                    ids = {"job_id": key, "client_id": r["client_id"]}
                case _:
                    ids = {"claim_status": key}
            if group_by == "status":
                name = str(key).capitalize()
            else:
                name = r["name"] if key is not None and r["name"] else _NO_EXPENSE_KEY[group_by]
            rows.append(
                ExpenseRow(
                    key=None if key is None else str(key),
                    code=r["code"],
                    name=name,
                    client_name=r["client_name"],
                    **ids,
                    **measures(r),
                )
            )
        if group_by == "status":
            rows.sort(key=lambda row: _CLAIM_STATUS_ORDER.index(row.key or ""))
        return rows

    # --- Payments -----------------------------------------------------------------------

    async def payments(self, ctx: OrgContext, q: PaymentsQuery) -> PaymentsReport:
        """``report.financial`` (checked by the route). Payments received in the range."""
        period = self._range(ctx, q.date_from, q.date_to)
        assert period.start is not None and period.end is not None
        clients = ItemFilter(
            client_id=q.client_id,
            partner_user_id=q.partner_user_id,
            manager_user_id=q.manager_user_id,
        )
        await self._check_refs(ctx, clients)
        found = await self.ext.payments(ctx.organization_id, period, q.group_by, clients, q.method)
        rows = self._payment_rows(q.group_by, period, found)
        totals = PaymentTotals(
            on_account=_sum(rows, "received") - _sum(rows, "allocated"),
            **{field: _sum(rows, field) for field in _PAYMENT_FIELDS},
        )
        filters = PaymentsFilters(
            date_from=period.start,
            date_to=period.end,
            group_by=q.group_by,
            client_id=q.client_id,
            partner_user_id=q.partner_user_id,
            manager_user_id=q.manager_user_id,
            method=q.method,
        )
        page_rows, page = window(rows, q)
        return PaymentsReport(rows=page_rows, totals=totals, filters=filters, page=page)

    @staticmethod
    def _payment_rows(
        group_by: str, period: DateRange, found: Sequence[RowMapping]
    ) -> list[PaymentRow]:
        def measures(r: RowMapping | None) -> dict[str, int]:
            values = {field: 0 if r is None else _int(r, field) for field in _PAYMENT_FIELDS}
            return {**values, "on_account": values["received"] - values["allocated"]}

        if group_by == "month":
            assert period.start is not None and period.end is not None
            by_month = {r["key"]: r for r in found}
            return [
                PaymentRow(
                    key=_month_key(month),
                    code=None,
                    name=_month_name(month),
                    **bucket_days("month", month, period),
                    **measures(by_month.get(month)),
                )
                for month in months_between(period.start, period.end)
            ]
        rows: list[PaymentRow] = []
        for r in found:
            key = r["key"]
            if group_by == "client":
                ids: dict[str, Any] = {"client_id": key}
                name = r["name"] or "Unknown client"
            else:
                ids = {"method": key}
                name = _METHOD_NAMES.get(str(key), str(key))
            rows.append(PaymentRow(key=str(key), code=r["code"], name=name, **ids, **measures(r)))
        return rows

    # --- Missing timesheets -------------------------------------------------------------

    async def missing_timesheets(
        self, ctx: OrgContext, q: MissingTimesheetsQuery
    ) -> MissingTimesheetsReport:
        """``report.read`` (checked by the route); who is listed follows ``GET
        /timesheets/missing``: everyone with ``time.read_all``, otherwise the people the caller
        manages."""
        start, end = self._weeks(ctx, q)
        user = None if q.user_id is None else str(q.user_id)
        found: list[MissingTimesheet] = []
        week = start
        while week <= end:
            found += await self.timesheets.missing(
                ctx, week_start=week, user_id=user, manager_user_id=q.manager_user_id
            )
            week += timedelta(days=7)
        wanted = set(q.status)
        rows = [self._missing_row(m) for m in found if not wanted or m.status in wanted]
        rows.sort(key=lambda row: (-row.week_start.toordinal(), row.name.lower(), str(row.user_id)))
        totals = MissingTimesheetsTotals(
            timesheets=len(rows),
            people=len({row.user_id for row in rows}),
            weeks=(end - start).days // 7 + 1,
            none_count=sum(row.status == "none" for row in rows),
            open_count=sum(row.status == "open" for row in rows),
            rejected_count=sum(row.status == "rejected" for row in rows),
            logged_minutes=_sum(rows, "logged_minutes"),
            expected_minutes=_sum(rows, "expected_minutes"),
            shortfall_minutes=_sum(rows, "shortfall_minutes"),
            missing_day_count=_sum(rows, "missing_day_count"),
        )
        filters = MissingTimesheetsFilters(
            date_from=start,
            date_to=end + timedelta(days=6),
            week_from=start,
            week_to=end,
            user_id=q.user_id,
            manager_user_id=q.manager_user_id,
            status=list(q.status),
        )
        page_rows, page = window(rows, q)
        return MissingTimesheetsReport(rows=page_rows, totals=totals, filters=filters, page=page)

    @staticmethod
    def _weeks(ctx: OrgContext, q: MissingTimesheetsQuery) -> tuple[date, date]:
        """First and last ``week_start`` of the report: the last completed week unless asked."""
        org = ctx.organization
        last = last_completed_week(org)
        first = None if q.week_from is None else week_start_for(org, q.week_from)
        final = None if q.week_to is None else week_start_for(org, q.week_to)
        if first is None and final is None:
            first = final = last
        elif first is None:
            first = final
        elif final is None:
            final = max(last, first)
        assert first is not None and final is not None
        if first > final:
            raise ValidationError.for_field("week_from", "week_from must be on or before week_to.")
        if final > week_start_for(org, practice_today(org)):
            raise ValidationError.for_field(
                "week_to", "A week that has not started cannot be reported on.", code="future_week"
            )
        if (final - first).days // 7 + 1 > MAX_MISSING_WEEKS:
            raise ValidationError.for_field(
                "week_to",
                f"A report covers at most {MAX_MISSING_WEEKS} weeks.",
                code="range_too_long",
            )
        return first, final

    @staticmethod
    def _missing_row(m: MissingTimesheet) -> MissingTimesheetRow:
        return MissingTimesheetRow(
            user_id=m.user_id,
            name=m.name,
            week_start=m.week_start,
            status=m.status,
            timesheet_id=m.timesheet_id,
            logged_minutes=m.logged_minutes,
            expected_minutes=m.expected_minutes,
            shortfall_minutes=max(m.expected_minutes - m.logged_minutes, 0),
            missing_day_count=len(m.missing_days),
            missing_days=m.missing_days,
            last_reminded_at=m.last_reminded_at,
            has_login=m.has_login,
        )

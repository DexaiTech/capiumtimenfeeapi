"""Reports: WIP, aged debtors, time, utilisation, recoverability, billing, profitability, VAT.

Read-only. Figures come straight from the ledger tables (time entries, disbursements,
WIP allocations, invoices, credit notes, payments); no other module's service is used.
Dates default to "today" in the practice's time zone. Without ``rate.read`` every cost
and margin figure is ``None``.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.db.models import Organization
from app.repositories.reports import DateRange, ItemFilter, ReportRepository
from app.schemas.reports import (
    MAX_RANGE_DAYS,
    AgedDebtorRow,
    AgedDebtorsQuery,
    AgedDebtorsReport,
    AgedDebtorTotals,
    BillingSummaryQuery,
    BillingSummaryReport,
    BillingSummaryRow,
    BillingSummaryTotals,
    JobProfitabilityQuery,
    JobProfitabilityReport,
    JobProfitabilityRow,
    JobProfitabilityTotals,
    RecoverabilityQuery,
    RecoverabilityReport,
    RecoverabilityRow,
    RecoverabilityTotals,
    ReportFilters,
    TimeSummaryQuery,
    TimeSummaryReport,
    TimeSummaryRow,
    TimeSummaryTotals,
    UtilisationQuery,
    UtilisationReport,
    UtilisationRow,
    UtilisationTotals,
    VatSummaryQuery,
    VatSummaryReport,
    VatSummaryRow,
    VatSummaryTotals,
    WipQuery,
    WipReport,
    WipRow,
    WipTotals,
)
from app.services.policy import OrgContext, has_permission
from app.services.report_paging import window
from app.utils.money import percentage_bp, round_half_up

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WIP_BUCKETS = ("age_0_30", "age_31_60", "age_61_90", "age_91_plus")
_DEBT_BUCKETS = ("current", "days_1_30", "days_31_60", "days_61_90", "days_91_plus")
_PAGING = {"format", "limit", "offset"}
_GROUP_ID_FIELD = {
    "person": "user_id",
    "client": "client_id",
    "job": "job_id",
    "activity": "activity_code_id",
    "service": "service_id",
}
_TIME_NO_KEY = {
    "client": "No client (internal time)",
    "job": "No job",
    "activity": "No activity code",
    "service": "No service",
    "person": "Unknown person",
}


def utc_now() -> datetime:
    """The current instant. Tests replace it to pin "today"."""
    return datetime.now(UTC)


def practice_today(organization: Organization) -> date:
    """Today's date where the practice is (a UTC evening may already be tomorrow there)."""
    return utc_now().astimezone(ZoneInfo(organization.timezone)).date()


def working_days_between(start: date, end: date, working_days: Iterable[int]) -> int:
    """How many days in ``[start, end]`` fall on one of the ISO weekdays ``working_days``."""
    weekdays = set(working_days)
    if end < start or not weekdays:
        return 0
    days = (end - start).days + 1
    full_weeks, remainder = divmod(days, 7)
    count = full_weeks * len(weekdays)
    tail_start = start + timedelta(days=full_weeks * 7)
    for offset in range(remainder):
        if (tail_start + timedelta(days=offset)).isoweekday() in weekdays:
            count += 1
    return count


def capacity_minutes(weekly_capacity: int, working_days: int, days_per_week: int) -> int:
    if days_per_week == 0:
        return 0
    return round_half_up(Decimal(weekly_capacity) * working_days / days_per_week)


def _int(row: RowMapping, key: str) -> int:
    return int(row[key] or 0)


def _str(value: object) -> str | None:
    return None if value is None else str(value)


def _sum(rows: Sequence[object], field: str) -> int:
    return sum(int(getattr(row, field)) for row in rows)


def _oldest(values: Iterable[date | None]) -> date | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


class ReportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.reports = ReportRepository(session)

    # --- shared -------------------------------------------------------------------------

    def _range(self, ctx: OrgContext, date_from: date | None, date_to: date | None) -> DateRange:
        """Defaults: up to today, from the first of the end date's month."""
        end = date_to or practice_today(ctx.organization)
        start = date_from or end.replace(day=1)
        self._check_order(start, end)
        return DateRange(start, end)

    @staticmethod
    def _check_order(start: date | None, end: date | None) -> None:
        if start is None or end is None:
            return
        if start > end:
            raise ValidationError.for_field("date_from", "date_from must be on or before date_to.")
        if (end - start).days >= MAX_RANGE_DAYS:
            raise ValidationError.for_field(
                "date_to", "A report covers at most ten years.", code="range_too_long"
            )

    async def _check_refs(self, ctx: OrgContext, f: ItemFilter) -> None:
        """Filter ids must exist in this practice; another practice's id answers 404."""
        org_id = ctx.organization_id
        if f.client_id is not None and not await self.reports.client_exists(org_id, f.client_id):
            raise NotFoundError("Client not found.")
        if f.job_id is not None and not await self.reports.job_exists(org_id, f.job_id):
            raise NotFoundError("Job not found.")
        for user_id in (f.user_id, f.partner_user_id, f.manager_user_id):
            if user_id is not None and not await self.reports.member_exists(org_id, user_id):
                raise NotFoundError("Member not found.")
        if f.service_id is not None and not await self.reports.service_exists(org_id, f.service_id):
            raise NotFoundError("Service not found.")
        if f.activity_code_id is not None and not await self.reports.activity_code_exists(
            org_id, f.activity_code_id
        ):
            raise NotFoundError("Activity code not found.")

    @staticmethod
    def _own_time_only(ctx: OrgContext, user_id: uuid.UUID | None) -> uuid.UUID | None:
        """Without ``time.read_all`` a person's time reports cover their own time only."""
        if has_permission(ctx, "time.read_all"):
            return user_id
        if user_id is not None and user_id != ctx.user.id:
            raise AuthorizationError("You can report on your own time only.")
        return ctx.user.id

    @staticmethod
    def _cost(ctx: OrgContext, value: int) -> int | None:
        return value if has_permission(ctx, "rate.read") else None

    # --- WIP ----------------------------------------------------------------------------

    async def wip(self, ctx: OrgContext, q: WipQuery) -> WipReport:
        as_at = q.as_at or practice_today(ctx.organization)
        f = ItemFilter(
            client_id=q.client_id,
            job_id=q.job_id,
            user_id=q.user_id,
            partner_user_id=q.partner_user_id,
            manager_user_id=q.manager_user_id,
            service_id=q.service_id,
        )
        await self._check_refs(ctx, f)
        found = await self.reports.wip(
            ctx.organization_id, as_at, ctx.organization.require_timesheet_approval, f
        )
        rows = [
            WipRow(
                client_id=r["client_id"],
                client_code=r["client_code"],
                client_name=r["client_name"],
                job_id=r["job_id"],
                job_number=r["job_number"],
                job_name=r["job_name"],
                oldest_item_date=r["oldest_item_date"],
                minutes=_int(r, "minutes"),
                time_value=_int(r, "time_value"),
                disbursement_value=_int(r, "disbursement_value"),
                total=_int(r, "total"),
                **{b: _int(r, b) for b in _WIP_BUCKETS},
            )
            for r in found
        ]
        totals = WipTotals(
            minutes=_sum(rows, "minutes"),
            time_value=_sum(rows, "time_value"),
            disbursement_value=_sum(rows, "disbursement_value"),
            total=_sum(rows, "total"),
            oldest_item_date=_oldest(r.oldest_item_date for r in rows),
            **{b: _sum(rows, b) for b in _WIP_BUCKETS},
        )
        filters = ReportFilters(as_at=as_at, **q.model_dump(exclude={"as_at", "format"}))
        return WipReport(rows=rows, totals=totals, filters=filters)

    # --- Aged debtors -------------------------------------------------------------------

    async def aged_debtors(self, ctx: OrgContext, q: AgedDebtorsQuery) -> AgedDebtorsReport:
        as_at = q.as_at or practice_today(ctx.organization)
        f = ItemFilter(
            client_id=q.client_id,
            partner_user_id=q.partner_user_id,
            manager_user_id=q.manager_user_id,
        )
        await self._check_refs(ctx, f)
        found = await self.reports.aged_debtors(ctx.organization_id, as_at, f)
        rows = [
            AgedDebtorRow(
                client_id=r["client_id"],
                client_code=r["client_code"],
                client_name=r["client_name"],
                oldest_due_date=r["oldest_due_date"],
                invoice_count=_int(r, "invoice_count"),
                total=_int(r, "total"),
                days_90_plus=_int(r, "days_91_plus"),
                **{b: _int(r, b) for b in _DEBT_BUCKETS},
            )
            for r in found
        ]
        totals = AgedDebtorTotals(
            invoice_count=_sum(rows, "invoice_count"),
            total=_sum(rows, "total"),
            days_90_plus=_sum(rows, "days_91_plus"),
            oldest_due_date=_oldest(r.oldest_due_date for r in rows),
            **{b: _sum(rows, b) for b in _DEBT_BUCKETS},
        )
        filters = ReportFilters(as_at=as_at, **q.model_dump(exclude={"as_at", *_PAGING}))
        page_rows, page = window(rows, q)
        return AgedDebtorsReport(rows=page_rows, totals=totals, filters=filters, page=page)

    # --- Time summary -------------------------------------------------------------------

    async def time_summary(self, ctx: OrgContext, q: TimeSummaryQuery) -> TimeSummaryReport:
        period = self._range(ctx, q.date_from, q.date_to)
        f = ItemFilter(
            client_id=q.client_id,
            job_id=q.job_id,
            user_id=q.user_id,
            partner_user_id=q.partner_user_id,
            manager_user_id=q.manager_user_id,
            service_id=q.service_id,
            activity_code_id=q.activity_code_id,
        )
        # Another practice's id answers 404 before the own-time rule can answer 403.
        await self._check_refs(ctx, f)
        f = replace(f, user_id=self._own_time_only(ctx, q.user_id))
        found = await self.reports.time_summary(
            ctx.organization_id, period, q.group_by, ctx.organization.week_start, f
        )
        rows = [self._time_row(ctx, q.group_by, period, r) for r in found]
        total_minutes = _sum(rows, "total_minutes")
        chargeable = _sum(rows, "chargeable_minutes")
        cost = sum(_int(r, "cost_value") for r in found)
        totals = TimeSummaryTotals(
            entry_count=_sum(rows, "entry_count"),
            total_minutes=total_minutes,
            chargeable_minutes=chargeable,
            non_chargeable_minutes=total_minutes - chargeable,
            chargeable_bp=percentage_bp(chargeable, total_minutes),
            charge_value=_sum(rows, "charge_value"),
            cost_value=self._cost(ctx, cost),
        )
        filters = ReportFilters(
            date_from=period.start,
            date_to=period.end,
            **q.model_dump(exclude={"date_from", "date_to", "user_id", *_PAGING}),
            user_id=f.user_id,
        )
        page_rows, page = window(rows, q)
        return TimeSummaryReport(rows=page_rows, totals=totals, filters=filters, page=page)

    def _time_row(
        self, ctx: OrgContext, group_by: str, period: DateRange, r: RowMapping
    ) -> TimeSummaryRow:
        key = r["key"]
        ids: dict[str, Any]
        if group_by in ("day", "week", "month"):
            iso = key.isoformat()
            name = {
                "day": iso,
                "week": f"Week of {iso}",
                "month": f"{_MONTHS[key.month - 1]} {key.year}",
            }[group_by]
            key_text: str | None = iso
            ids = bucket_days(group_by, key, period)
        else:
            key_text = _str(key)
            name = r["name"] if key is not None and r["name"] else _TIME_NO_KEY[group_by]
            ids = _group_ids(group_by, key, r["client_id"])
        total_minutes = _int(r, "total_minutes")
        chargeable = _int(r, "chargeable_minutes")
        return TimeSummaryRow(
            key=key_text,
            code=r["code"],
            name=name,
            client_name=r["client_name"],
            entry_count=_int(r, "entry_count"),
            total_minutes=total_minutes,
            chargeable_minutes=chargeable,
            non_chargeable_minutes=total_minutes - chargeable,
            chargeable_bp=percentage_bp(chargeable, total_minutes),
            charge_value=_int(r, "charge_value"),
            cost_value=self._cost(ctx, _int(r, "cost_value")),
            **ids,
        )

    # --- Utilisation --------------------------------------------------------------------

    async def utilisation(self, ctx: OrgContext, q: UtilisationQuery) -> UtilisationReport:
        period = self._range(ctx, q.date_from, q.date_to)
        assert period.start is not None and period.end is not None
        await self._check_refs(
            ctx, ItemFilter(user_id=q.user_id, manager_user_id=q.manager_user_id)
        )
        user_id = self._own_time_only(ctx, q.user_id)
        found = await self.reports.utilisation(
            ctx.organization_id, period, user_id=user_id, manager_user_id=q.manager_user_id
        )
        org_days = ctx.organization.working_days
        working = working_days_between(period.start, period.end, org_days)
        per_week = len(set(org_days))
        rows: list[UtilisationRow] = []
        weighted_target = 0
        for r in found:
            capacity = capacity_minutes(r["weekly_capacity_minutes"], working, per_week)
            chargeable = _int(r, "chargeable_minutes")
            total_minutes = _int(r, "total_minutes")
            target = int(r["chargeable_target_bp"])
            weighted_target += target * capacity
            utilisation = percentage_bp(chargeable, capacity)
            rows.append(
                UtilisationRow(
                    user_id=r["user_id"],
                    full_name=r["full_name"],
                    initials=r["initials"],
                    status=r["status"],
                    capacity_minutes=capacity,
                    total_minutes=total_minutes,
                    chargeable_minutes=chargeable,
                    non_chargeable_minutes=total_minutes - chargeable,
                    utilisation_bp=utilisation,
                    target_bp=target,
                    variance_bp=None if utilisation is None else utilisation - target,
                )
            )
        capacity_total = _sum(rows, "capacity_minutes")
        chargeable_total = _sum(rows, "chargeable_minutes")
        minutes_total = _sum(rows, "total_minutes")
        utilisation_total = percentage_bp(chargeable_total, capacity_total)
        # The practice target is the capacity-weighted average of the people's targets.
        target_total = (
            round_half_up(Decimal(weighted_target) / capacity_total) if capacity_total else None
        )
        totals = UtilisationTotals(
            working_days=working,
            capacity_minutes=capacity_total,
            total_minutes=minutes_total,
            chargeable_minutes=chargeable_total,
            non_chargeable_minutes=minutes_total - chargeable_total,
            utilisation_bp=utilisation_total,
            target_bp=target_total,
            variance_bp=(
                utilisation_total - target_total
                if utilisation_total is not None and target_total is not None
                else None
            ),
        )
        filters = ReportFilters(
            date_from=period.start,
            date_to=period.end,
            user_id=user_id,
            manager_user_id=q.manager_user_id,
        )
        page_rows, page = window(rows, q)
        return UtilisationReport(rows=page_rows, totals=totals, filters=filters, page=page)

    # --- Recoverability -----------------------------------------------------------------

    async def recoverability(self, ctx: OrgContext, q: RecoverabilityQuery) -> RecoverabilityReport:
        period = self._range(ctx, q.date_from, q.date_to)
        f = ItemFilter(
            client_id=q.client_id,
            job_id=q.job_id,
            user_id=q.user_id,
            partner_user_id=q.partner_user_id,
            manager_user_id=q.manager_user_id,
            service_id=q.service_id,
        )
        await self._check_refs(ctx, f)
        found = await self.reports.recoverability(ctx.organization_id, period, q.group_by, f)
        no_key = {"client": "Unknown client", "job": "No job", "person": "Disbursements"}
        rows = [
            RecoverabilityRow(
                key=_str(r["key"]),
                code=r["code"],
                name=r["name"] if r["key"] is not None and r["name"] else no_key[q.group_by],
                client_name=r["client_name"],
                **_group_ids(q.group_by, r["key"], r["client_id"]),
                **_recovery(
                    _int(r, "billed_wip_value"),
                    _int(r, "billed_value"),
                    _int(r, "written_off_value"),
                ),
            )
            for r in found
        ]
        totals = RecoverabilityTotals(
            **_recovery(
                _sum(rows, "billed_wip_value"),
                _sum(rows, "billed_value"),
                _sum(rows, "written_off_value"),
            )
        )
        filters = ReportFilters(
            date_from=period.start,
            date_to=period.end,
            **q.model_dump(exclude={"date_from", "date_to", *_PAGING}),
        )
        page_rows, page = window(rows, q)
        return RecoverabilityReport(rows=page_rows, totals=totals, filters=filters, page=page)

    # --- Billing summary ----------------------------------------------------------------

    async def billing_summary(
        self, ctx: OrgContext, q: BillingSummaryQuery
    ) -> BillingSummaryReport:
        period = self._range(ctx, q.date_from, q.date_to)
        assert period.start is not None and period.end is not None
        f = ItemFilter(
            client_id=q.client_id,
            partner_user_id=q.partner_user_id,
            manager_user_id=q.manager_user_id,
        )
        await self._check_refs(ctx, f)
        invoices, credits, receipts = await self.reports.billing_documents(
            ctx.organization_id, period, q.group_by, f
        )
        by_key: dict[object, dict[str, int]] = {}

        def bucket(key: object) -> dict[str, int]:
            return by_key.setdefault(key, dict.fromkeys(_BILLING_FIELDS, 0))

        for r in invoices:
            b = bucket(r["key"])
            b["invoice_count"] = _int(r, "count")
            b["invoiced_net"], b["invoiced_vat"] = _int(r, "net"), _int(r, "vat")
            b["invoiced_gross"] = _int(r, "gross")
        for r in credits:
            b = bucket(r["key"])
            b["credit_note_count"] = _int(r, "count")
            b["credited_net"], b["credited_vat"] = _int(r, "net"), _int(r, "vat")
            b["credited_gross"] = _int(r, "gross")
        for r in receipts:
            bucket(r["key"])["receipts"] = _int(r, "amount")

        rows: list[BillingSummaryRow] = []
        if q.group_by == "month":
            for month in months_between(period.start, period.end):
                amounts = by_key.get(month) or dict.fromkeys(_BILLING_FIELDS, 0)
                rows.append(
                    BillingSummaryRow(
                        key=f"{month.year:04d}-{month.month:02d}",
                        name=f"{_MONTHS[month.month - 1]} {month.year}",
                        net_billed=amounts["invoiced_net"] - amounts["credited_net"],
                        **bucket_days("month", month, period),
                        **amounts,
                    )
                )
        else:
            partner_ids = [k for k in by_key if isinstance(k, uuid.UUID)]
            names = await self.reports.member_names(ctx.organization_id, partner_ids)
            for key, amounts in by_key.items():
                partner = key if isinstance(key, uuid.UUID) else None
                rows.append(
                    BillingSummaryRow(
                        key=_str(partner),
                        name=names.get(partner, "Unknown") if partner else "No partner",
                        partner_user_id=partner,
                        net_billed=amounts["invoiced_net"] - amounts["credited_net"],
                        **amounts,
                    )
                )
            rows.sort(key=lambda row: (row.key is None, row.name.lower(), row.key or ""))
        totals = BillingSummaryTotals(
            net_billed=_sum(rows, "net_billed"),
            **{field: _sum(rows, field) for field in _BILLING_FIELDS},
        )
        filters = ReportFilters(
            date_from=period.start,
            date_to=period.end,
            **q.model_dump(exclude={"date_from", "date_to", *_PAGING}),
        )
        page_rows, page = window(rows, q)
        return BillingSummaryReport(rows=page_rows, totals=totals, filters=filters, page=page)

    # --- Job profitability --------------------------------------------------------------

    async def job_profitability(
        self, ctx: OrgContext, q: JobProfitabilityQuery
    ) -> JobProfitabilityReport:
        self._check_order(q.date_from, q.date_to)
        period = DateRange(q.date_from, q.date_to)
        f = ItemFilter(
            client_id=q.client_id,
            job_id=q.job_id,
            partner_user_id=q.partner_user_id,
            manager_user_id=q.manager_user_id,
            service_id=q.service_id,
        )
        await self._check_refs(ctx, f)
        found = await self.reports.job_profitability(ctx.organization_id, period, f)
        can_cost = has_permission(ctx, "rate.read")
        rows: list[JobProfitabilityRow] = []
        for r in found:
            logged, time_value = _int(r, "logged_minutes"), _int(r, "time_value")
            budget_minutes, budget_fee = r["budget_minutes"], r["budget_fee"]
            rows.append(
                JobProfitabilityRow(
                    job_id=r["job_id"],
                    job_number=r["job_number"],
                    job_name=r["job_name"],
                    status=r["status"],
                    billing_basis=r["billing_basis"],
                    client_id=r["client_id"],
                    client_code=r["client_code"],
                    client_name=r["client_name"],
                    fixed_fee=r["fixed_fee"],
                    budget_minutes=budget_minutes,
                    budget_minutes_used_bp=(
                        percentage_bp(logged, budget_minutes) if budget_minutes else None
                    ),
                    budget_fee=budget_fee,
                    budget_fee_used_bp=percentage_bp(time_value, budget_fee)
                    if budget_fee
                    else None,
                    **_profit(
                        can_cost,
                        logged_minutes=logged,
                        time_value=time_value,
                        cost_value=_int(r, "cost_value"),
                        billed_time_value=_int(r, "billed_time_value"),
                        billed_disbursement_value=_int(r, "billed_disbursement_value"),
                        written_off_value=_int(r, "written_off_value"),
                    ),
                )
            )
        totals = JobProfitabilityTotals(
            **_profit(
                can_cost,
                logged_minutes=_sum(rows, "logged_minutes"),
                time_value=_sum(rows, "time_value"),
                cost_value=sum(_int(r, "cost_value") for r in found),
                billed_time_value=_sum(rows, "billed_time_value"),
                billed_disbursement_value=_sum(rows, "billed_disbursement_value"),
                written_off_value=_sum(rows, "written_off_value"),
            )
        )
        filters = ReportFilters(**q.model_dump(exclude=_PAGING))
        page_rows, page = window(rows, q)
        return JobProfitabilityReport(rows=page_rows, totals=totals, filters=filters, page=page)

    # --- VAT summary --------------------------------------------------------------------

    async def vat_summary(self, ctx: OrgContext, q: VatSummaryQuery) -> VatSummaryReport:
        period = self._range(ctx, q.date_from, q.date_to)
        await self._check_refs(ctx, ItemFilter(client_id=q.client_id))
        invoiced, credited = await self.reports.vat_summary(
            ctx.organization_id, period, q.client_id
        )
        merged: dict[tuple[uuid.UUID | None, int], dict[str, object]] = {}
        for source, prefix in ((invoiced, "invoiced"), (credited, "credited")):
            for r in source:
                entry = merged.setdefault(
                    (r["vat_rate_id"], int(r["rate_bp"])),
                    {
                        "name": r["name"],
                        "code": r["code"],
                        "invoiced_net": 0,
                        "invoiced_vat": 0,
                        "credited_net": 0,
                        "credited_vat": 0,
                    },
                )
                entry[f"{prefix}_net"] = _int(r, "net")
                entry[f"{prefix}_vat"] = _int(r, "vat")
        rows = []
        for (rate_id, rate_bp), e in merged.items():
            amounts = {k: int(str(e[k])) for k in _VAT_FIELDS}
            rows.append(
                VatSummaryRow(
                    vat_rate_id=rate_id,
                    name=str(e["name"]) if e["name"] else "No VAT rate",
                    code=_str(e["code"]),
                    rate_bp=rate_bp,
                    net=amounts["invoiced_net"] - amounts["credited_net"],
                    vat=amounts["invoiced_vat"] - amounts["credited_vat"],
                    **amounts,
                )
            )
        rows.sort(key=lambda row: (-row.rate_bp, row.name.lower(), str(row.vat_rate_id)))
        totals = VatSummaryTotals(
            net=_sum(rows, "net"),
            vat=_sum(rows, "vat"),
            **{k: _sum(rows, k) for k in _VAT_FIELDS},
        )
        filters = ReportFilters(date_from=period.start, date_to=period.end, client_id=q.client_id)
        page_rows, page = window(rows, q)
        return VatSummaryReport(rows=page_rows, totals=totals, filters=filters, page=page)


_BILLING_FIELDS = (
    "invoice_count",
    "invoiced_net",
    "invoiced_vat",
    "invoiced_gross",
    "credit_note_count",
    "credited_net",
    "credited_vat",
    "credited_gross",
    "receipts",
)
_VAT_FIELDS = ("invoiced_net", "invoiced_vat", "credited_net", "credited_vat")


def months_between(start: date, end: date) -> list[date]:
    months: list[date] = []
    month = start.replace(day=1)
    while month <= end:
        months.append(month)
        month = (month + timedelta(days=32)).replace(day=1)
    return months


def _group_ids(group_by: str, key: object, client_id: object = None) -> dict[str, Any]:
    """The id of a row's group under the name the module filters use (a job row names its
    client too), for the drill-down to the source records."""
    ids: dict[str, Any] = {_GROUP_ID_FIELD[group_by]: key}
    if group_by == "job":
        ids["client_id"] = client_id
    return ids


def bucket_days(group_by: str, start: date, period: DateRange) -> dict[str, date]:
    """First and last day of a day, week or month row, cut to the report's range."""
    if group_by == "day":
        last = start
    elif group_by == "week":
        last = start + timedelta(days=6)
    else:
        last = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    first = start if period.start is None else max(start, period.start)
    end = last if period.end is None else min(last, period.end)
    return {"date_from": first, "date_to": end}


def _recovery(billed_wip: int, billed: int, written_off: int) -> dict[str, int | None]:
    wip_value = billed_wip + written_off
    return {
        "billed_wip_value": billed_wip,
        "billed_value": billed,
        "billing_adjustment": billed - billed_wip,
        "written_off_value": written_off,
        "wip_value": wip_value,
        "net_adjustment": billed - wip_value,
        "recovery_bp": percentage_bp(billed, wip_value),
    }


def _profit(
    can_cost: bool,
    *,
    logged_minutes: int,
    time_value: int,
    cost_value: int,
    billed_time_value: int,
    billed_disbursement_value: int,
    written_off_value: int,
) -> dict[str, int | None]:
    margin = billed_time_value - cost_value if can_cost else None
    return {
        "logged_minutes": logged_minutes,
        "time_value": time_value,
        "cost_value": cost_value if can_cost else None,
        "billed_time_value": billed_time_value,
        "billed_disbursement_value": billed_disbursement_value,
        "billed_value": billed_time_value + billed_disbursement_value,
        "written_off_value": written_off_value,
        "margin": margin,
        "margin_bp": None if margin is None else percentage_bp(margin, billed_time_value),
    }

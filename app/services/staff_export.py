"""Every staff member as CSV (``GET /staff/export``), in the columns the staff import reads."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.staff import StaffFilters, StaffRepository, StaffRow, normalised_status
from app.schemas.staff import login_status
from app.services import charge_rates, policy
from app.services.charge_rates import RateQuery, ResolvedRate
from app.services.import_csv import csv_bytes, pounds
from app.services.policy import OrgContext
from app.services.staff_import import format_working_days
from app.utils.pagination import PageParams
from app.utils.practice_dates import today_in

BATCH = 100
RATE_BATCH = 500
HEADER = (
    "code",
    "full_name",
    "email",
    "grade",
    "job_title",
    "initials",
    "start_date",
    "leaving_date",
    "working_minutes_per_day",
    "working_days",
    "chargeable_target_percent",
    "approver_code",
    "status",
    "login_status",
)
RATE_HEADER = ("cost_rate", "charge_rate")


def _percent(bp: int) -> str:
    whole, fraction = divmod(bp, 100)
    return f"{whole}.{fraction:02d}".rstrip("0").rstrip(".")


class StaffExportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.staff = StaffRepository(session)

    async def _all(self, org_id: uuid.UUID) -> list[StaffRow]:
        rows: list[StaffRow] = []
        cursor: str | None = None
        while True:
            page = await self.staff.list_page(
                org_id,
                StaffFilters(),
                sort="code",
                descending=False,
                params=PageParams(limit=BATCH, cursor=cursor),
            )
            rows.extend(page.items)
            if page.next_cursor is None:
                return rows
            cursor = page.next_cursor

    async def export_csv(self, ctx: OrgContext) -> bytes:
        """Every staff member (any status), by code. The current standard cost and charge-out
        rates (pounds per hour, as ``GET /staff`` resolves them) only with ``rate.read``."""
        rows = await self._all(ctx.organization_id)
        show_rates = policy.has_permission(ctx, "rate.read")
        rates: list[ResolvedRate | None] = [None] * len(rows)
        if show_rates and rows:
            today = today_in(ctx.organization.timezone)
            resolved: list[ResolvedRate] = []
            for start in range(0, len(rows), RATE_BATCH):
                chunk = rows[start : start + RATE_BATCH]
                resolved += await charge_rates.resolve_many(
                    self.session,
                    ctx.organization,
                    [RateQuery(r.member.user_id, None, None, today) for r in chunk],
                )
            rates = list(resolved)
        codes = {r.member.user_id: r.member.staff_code for r in rows}
        body = []
        for row, rate in zip(rows, rates, strict=True):
            member = row.member
            line: list[object] = [
                member.staff_code,
                row.user.full_name,
                member.work_email,
                row.grade.code if row.grade else None,
                member.job_title,
                member.initials,
                member.start_date,
                member.leaving_date,
                member.working_minutes_per_day,
                format_working_days(list(member.working_days)),
                _percent(member.chargeable_target_bp),
                codes.get(member.manager_user_id) if member.manager_user_id else None,
                normalised_status(member.status),
                login_status(row),
            ]
            if show_rates:
                line += [
                    pounds(rate.cost_rate) if rate else None,
                    pounds(rate.charge_rate) if rate else None,
                ]
            body.append(line)
        return csv_bytes(HEADER + RATE_HEADER if show_rates else HEADER, body)

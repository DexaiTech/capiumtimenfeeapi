"""Every job as CSV (``GET /jobs/export``), in the columns the jobs import reads."""

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrganizationMember, Service
from app.repositories.time_jobs import ClientJobRepository, JobFilters, JobView
from app.services.import_csv import csv_bytes, pounds
from app.services.policy import OrgContext
from app.utils.pagination import PageParams

BATCH = 100
HEADER = (
    "number",
    "name",
    "client_code",
    "service_code",
    "status",
    "billing_basis",
    "fixed_fee",
    "hourly_rate",
    "budget_hours",
    "budget_fee",
    "period_start",
    "period_end",
    "start_date",
    "due_date",
    "manager",
    "partner",
    "assignees",
    "description",
    "client_name",
)


def _hours(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    return format((Decimal(minutes) / 60).quantize(Decimal("0.01")).normalize(), "f")


class JobExportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.jobs = ClientJobRepository(session)

    async def _views(self, org_id: uuid.UUID) -> list[JobView]:
        views: list[JobView] = []
        cursor: str | None = None
        while True:
            page = await self.jobs.list_page(
                org_id, JobFilters(), "number", PageParams(limit=BATCH, cursor=cursor)
            )
            views.extend(page.items)
            if page.next_cursor is None:
                return views
            cursor = page.next_cursor

    async def export_csv(self, ctx: OrgContext) -> bytes:
        """Every job that is not deleted, by number, with the people as staff codes and the
        service as its code. Money is in pounds, the budget in hours. No actuals or WIP."""
        org_id = ctx.organization_id
        views = await self._views(org_id)
        member_rows = await self.session.execute(
            select(OrganizationMember.user_id, OrganizationMember.staff_code).where(
                OrganizationMember.organization_id == org_id
            )
        )
        codes = dict(member_rows.all())
        service_rows = await self.session.execute(
            select(Service.id, Service.code).where(Service.organization_id == org_id)
        )
        services = dict(service_rows.all())
        assignees = await self.jobs.assignees(org_id, [v.job.id for v in views])
        rows = []
        for view in views:
            job = view.job
            rows.append(
                [
                    job.number,
                    job.name,
                    view.client_code,
                    services.get(job.service_id) if job.service_id else None,
                    job.status,
                    job.billing_basis,
                    pounds(job.fixed_fee),
                    pounds(job.hourly_rate),
                    _hours(job.budget_minutes),
                    pounds(job.budget_fee),
                    job.period_start,
                    job.period_end,
                    job.start_date,
                    job.due_date,
                    codes.get(job.manager_user_id) if job.manager_user_id else None,
                    codes.get(job.partner_user_id) if job.partner_user_id else None,
                    ";".join(codes.get(u, "") for u in assignees.get(job.id, []) if codes.get(u)),
                    job.description,
                    view.client_name,
                ]
            )
        return csv_bytes(HEADER, rows)

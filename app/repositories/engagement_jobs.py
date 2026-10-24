"""Engagement job runs and the engagements the daily worker looks at."""

import uuid
from collections.abc import Collection
from datetime import date, timedelta

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import OWNER_KEY
from app.db.models import (
    ClientService,
    EngagementJobRun,
    Job,
    OrganizationMember,
    Role,
    Service,
)

MAX_LEAD_DAYS = 365


class EngagementJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def organizations_with_schedules(self, horizon: date) -> list[uuid.UUID]:
        """Practices with an active, opted-in engagement that may be due (``horizon`` is a day
        ahead of UTC today so every time zone is covered; each practice re-checks in its own)."""
        stmt = (
            select(ClientService.organization_id)
            .where(*self._schedulable(horizon))
            .distinct()
            .order_by(ClientService.organization_id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def schedulable_ids(self, organization_id: uuid.UUID, today: date) -> list[uuid.UUID]:
        stmt = (
            select(ClientService.id)
            .where(
                ClientService.organization_id == organization_id,
                *self._schedulable(today + timedelta(days=1)),
            )
            .order_by(ClientService.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    @staticmethod
    def _schedulable(horizon: date) -> list:  # type: ignore[type-arg]
        return [
            ClientService.auto_create_jobs.is_(True),
            ClientService.is_active.is_(True),
            ClientService.start_date.is_not(None),
            # The window of the first period opens at most ``MAX_LEAD_DAYS`` before its start.
            ClientService.start_date <= horizon + timedelta(days=MAX_LEAD_DAYS),
        ]

    async def lock_engagement(
        self,
        organization_id: uuid.UUID,
        engagement_id: uuid.UUID,
        *,
        skip_locked: bool,
        lock: bool = True,
    ) -> tuple[ClientService, Service] | None:
        stmt = (
            select(ClientService, Service)
            .join(
                Service,
                and_(
                    Service.organization_id == ClientService.organization_id,
                    Service.id == ClientService.service_id,
                ),
            )
            .where(
                ClientService.organization_id == organization_id,
                ClientService.id == engagement_id,
            )
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update(of=ClientService, skip_locked=skip_locked)
        row = (await self.session.execute(stmt)).one_or_none()
        return (row[0], row[1]) if row else None

    async def oldest_owner(self, organization_id: uuid.UUID) -> OrganizationMember | None:
        """The practice's oldest active Owner (one who can sign in first): the person the
        worker acts as."""
        stmt = (
            select(OrganizationMember)
            .join(Role, Role.id == OrganizationMember.role_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.status == "active",
                Role.system_key == OWNER_KEY,
            )
            .order_by(
                OrganizationMember.login_enabled.desc(),
                OrganizationMember.created_at,
                OrganizationMember.user_id,
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_run(
        self, engagement_id: uuid.UUID, period_start: date
    ) -> EngagementJobRun | None:
        stmt = select(EngagementJobRun).where(
            EngagementJobRun.client_service_id == engagement_id,
            EngagementJobRun.period_start == period_start,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_run(self, run: EngagementJobRun) -> EngagementJobRun:
        self.session.add(run)
        await self.session.flush()
        return run

    async def created_periods(
        self, organization_id: uuid.UUID, engagement_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, set[date]]:
        """The period starts that already have their job, per engagement."""
        found: dict[uuid.UUID, set[date]] = {}
        if not engagement_ids:
            return found
        stmt = select(EngagementJobRun.client_service_id, EngagementJobRun.period_start).where(
            EngagementJobRun.organization_id == organization_id,
            EngagementJobRun.client_service_id.in_(list(engagement_ids)),
            EngagementJobRun.status == "created",
        )
        for engagement_id, start in (await self.session.execute(stmt)).all():
            found.setdefault(engagement_id, set()).add(start)
        return found

    async def runs(
        self, organization_id: uuid.UUID, engagement_id: uuid.UUID, limit: int
    ) -> list[tuple[EngagementJobRun, Job | None]]:
        """Newest period first; the job is ``None`` once it was deleted."""
        stmt = (
            select(EngagementJobRun, Job)
            .outerjoin(
                Job,
                and_(
                    Job.organization_id == EngagementJobRun.organization_id,
                    Job.id == EngagementJobRun.job_id,
                    Job.deleted_at.is_(None),
                ),
            )
            .where(
                EngagementJobRun.organization_id == organization_id,
                EngagementJobRun.client_service_id == engagement_id,
            )
            .order_by(EngagementJobRun.period_start.desc())
            .limit(limit)
        )
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

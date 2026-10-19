"""Queries behind the practice management integration: jobs and tasks by their PM ids, the matches
for an import (client by central id, service by code, member by email, an unlinked job of the same
name) and the rows to link. Every method is scoped to one organization."""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, Job, OrganizationMember, Service, Task, User


class PmLinkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def job_by_pm_id(
        self, organization_id: uuid.UUID, pm_job_id: str, *, lock: bool = False
    ) -> Job | None:
        stmt = select(Job).where(
            Job.organization_id == organization_id,
            Job.pm_job_id == pm_job_id,
            Job.deleted_at.is_(None),
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def job(
        self, organization_id: uuid.UUID, job_id: uuid.UUID, *, lock: bool = False
    ) -> Job | None:
        stmt = select(Job).where(
            Job.organization_id == organization_id, Job.id == job_id, Job.deleted_at.is_(None)
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def unlinked_job_named(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, name: str
    ) -> Job | None:
        """The oldest live job of the client with this name (any case) that is not linked."""
        stmt = (
            select(Job)
            .where(
                Job.organization_id == organization_id,
                Job.client_id == client_id,
                Job.pm_job_id.is_(None),
                Job.deleted_at.is_(None),
                func.lower(Job.name) == name.lower(),
            )
            .order_by(Job.created_at, Job.id)
            .limit(1)
            .with_for_update()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def client_by_central_id(
        self, organization_id: uuid.UUID, central_client_id: str
    ) -> Client | None:
        """The live client of the practice linked to this central client."""
        stmt = select(Client).where(
            Client.organization_id == organization_id,
            Client.central_client_id == central_client_id,
            Client.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def client_of(self, job: Job) -> Client | None:
        stmt = select(Client).where(
            Client.organization_id == job.organization_id, Client.id == job.client_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def service_by_code(self, organization_id: uuid.UUID, code: str) -> Service | None:
        stmt = select(Service).where(
            Service.organization_id == organization_id,
            func.upper(Service.code) == code.upper(),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def active_member_id_by_email(
        self, organization_id: uuid.UUID, email: str
    ) -> uuid.UUID | None:
        """The user id of the active member who signs in with, or has as work email, this address."""
        lowered = email.strip().lower()
        stmt = (
            select(OrganizationMember.user_id)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.status == "active",
                or_(
                    func.lower(User.email) == lowered,
                    func.lower(OrganizationMember.work_email) == lowered,
                ),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def tasks_of_job(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> list[Task]:
        stmt = (
            select(Task)
            .where(Task.organization_id == organization_id, Task.job_id == job_id)
            .order_by(Task.sort_order, Task.created_at, Task.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def task_by_pm_id(self, organization_id: uuid.UUID, pm_task_id: str) -> Task | None:
        stmt = select(Task).where(
            Task.organization_id == organization_id, Task.pm_task_id == pm_task_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def next_sort_order(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.max(Task.sort_order) + 1, 0)).where(
            Task.organization_id == organization_id, Task.job_id == job_id
        )
        return int((await self.session.execute(stmt)).scalar_one())

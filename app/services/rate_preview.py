"""Rate preview: which rate would apply, and why."""

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.repositories.clients import ClientRepository
from app.repositories.grades import GradeRepository
from app.repositories.members import MemberRepository
from app.repositories.settings import ServiceRepository
from app.repositories.time_rates import RateLookupRepository
from app.schemas.rate_rules import RefRead, ResolveRead, StaffRef
from app.services import charge_rates, policy
from app.services.charge_rates import RateQuery
from app.services.policy import OrgContext
from app.utils.practice_dates import today_in


class RatePreviewService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.members = MemberRepository(session)
        self.clients = ClientRepository(session)
        self.services = ServiceRepository(session)
        self.grades = GradeRepository(session)
        self.lookups = RateLookupRepository(session)

    async def resolve(
        self,
        ctx: OrgContext,
        *,
        user_id: uuid.UUID,
        on_date: date | None,
        client_id: uuid.UUID | None,
        job_id: uuid.UUID | None,
        service_id: uuid.UUID | None,
    ) -> ResolveRead:
        """The charge-out and cost rate for a person on a day, with all levels of both ladders.

        With a job, the client and service default to the job's own; a given service overrides
        the job's (what-if). Nothing is written.
        """
        org = ctx.organization
        org_id = org.id
        day = on_date or today_in(org.timezone)
        member = await self.members.get(org_id, user_id)
        if member is None:
            raise NotFoundError("Staff member not found.")
        job = None
        if job_id is not None:
            job = (await self.lookups.jobs(org_id, [job_id])).get(job_id)
            if job is None:
                raise NotFoundError("Job not found.")
            if client_id is not None and client_id != job.client_id:
                raise ValidationError.for_field("job_id", "This job belongs to another client.")
            client_id = job.client_id
        client = None
        if client_id is not None:
            client = await self.clients.get(org_id, client_id)
            if client is None:
                raise NotFoundError("Client not found.")
        service = None
        service_ref = service_id or (job.service_id if job else None)
        if service_ref is not None:
            service = await self.services.get(org_id, service_ref)
            if service is None:
                raise NotFoundError("Service not found.")
        grade = None
        if member.grade_id is not None:
            grade = await self.grades.get(org_id, member.grade_id)

        explanation = await charge_rates.explain(
            self.session,
            org,
            RateQuery(
                user_id,
                client.id if client else None,
                job_id,
                day,
                service.id if service else None,
            ),
        )
        return ResolveRead.build(
            day=day,
            staff=StaffRef(id=user_id, code=member.staff_code, full_name=member.user.full_name),
            client=RefRead(id=client.id, code=client.code, name=client.name) if client else None,
            service=RefRead(id=service.id, code=service.code, name=service.name)
            if service
            else None,
            job_id=job_id,
            grade=RefRead(id=grade.id, code=grade.code, name=grade.name) if grade else None,
            explanation=explanation,
            show_cost=policy.has_permission(ctx, "rate.read"),
        )

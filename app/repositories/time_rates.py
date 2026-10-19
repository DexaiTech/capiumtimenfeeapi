"""Queries behind rate resolution. Scoped to one organization."""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Grade, Job, OrganizationMember, RateRule

# (kind, client_id, service_id, user_id, grade_id): the scope of a rate rule.
RuleKey = tuple[str, uuid.UUID | None, uuid.UUID | None, uuid.UUID | None, uuid.UUID | None]


@dataclass(frozen=True, slots=True)
class RuleRow:
    """A rate rule as the resolver needs it."""

    id: uuid.UUID
    rate: int
    effective_from: date
    effective_to: date | None

    def covers(self, day: date) -> bool:
        return self.effective_from <= day and (
            self.effective_to is None or day <= self.effective_to
        )


@dataclass(frozen=True, slots=True)
class JobRate:
    client_id: uuid.UUID
    service_id: uuid.UUID | None
    hourly_rate: int | None


class RateLookupRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def jobs(
        self, organization_id: uuid.UUID, job_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, JobRate]:
        """Client, service and rate of each job (deleted jobs included: their entries remain)."""
        if not job_ids:
            return {}
        stmt = select(Job.id, Job.client_id, Job.service_id, Job.hourly_rate).where(
            Job.organization_id == organization_id, Job.id.in_(list(job_ids))
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: JobRate(row[1], row[2], row[3]) for row in rows}

    async def member_grades(
        self, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID | None]:
        """The current grade of each person (null when they have none)."""
        if not user_ids:
            return {}
        stmt = select(OrganizationMember.user_id, OrganizationMember.grade_id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id.in_(list(user_ids)),
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    async def grade_defaults(
        self, organization_id: uuid.UUID, grade_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[int | None, int | None]]:
        """``grade_id -> (default_charge_rate, default_cost_rate)``."""
        if not grade_ids:
            return {}
        stmt = select(Grade.id, Grade.default_charge_rate, Grade.default_cost_rate).where(
            Grade.organization_id == organization_id, Grade.id.in_(list(grade_ids))
        )
        return {row[0]: (row[1], row[2]) for row in (await self.session.execute(stmt)).all()}

    async def rules(
        self,
        organization_id: uuid.UUID,
        *,
        client_ids: Collection[uuid.UUID],
        user_ids: Collection[uuid.UUID],
        grade_ids: Collection[uuid.UUID],
    ) -> dict[RuleKey, list[RuleRow]]:
        """Every rule that names one of these clients, people or grades, by scope, oldest first."""
        conditions = []
        if client_ids:
            conditions.append(RateRule.client_id.in_(list(client_ids)))
        if user_ids:
            conditions.append(RateRule.user_id.in_(list(user_ids)))
        if grade_ids:
            conditions.append(RateRule.grade_id.in_(list(grade_ids)))
        if not conditions:
            return {}
        stmt = (
            select(
                RateRule.id,
                RateRule.kind,
                RateRule.client_id,
                RateRule.service_id,
                RateRule.user_id,
                RateRule.grade_id,
                RateRule.rate,
                RateRule.effective_from,
                RateRule.effective_to,
            )
            .where(RateRule.organization_id == organization_id, or_(*conditions))
            .order_by(RateRule.effective_from)
        )
        found: dict[RuleKey, list[RuleRow]] = {}
        for row in (await self.session.execute(stmt)).all():
            key: RuleKey = (row[1], row[2], row[3], row[4], row[5])
            found.setdefault(key, []).append(RuleRow(row[0], row[6], row[7], row[8]))
        return found

"""Queries behind the rates of a client's jobs (APT54). Scoped to one organization."""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job, RateRule

# The levels a client's job can be priced at before its own rate and after it (docs/domain.md).
CLIENT_LEVELS = ("client_service_staff", "client_service", "client_staff", "client")
TASK_LEVELS = ("service_staff", "service_grade")


@dataclass(frozen=True, slots=True)
class JobTermsRow:
    id: uuid.UUID
    service_id: uuid.UUID | None
    billing_basis: str
    fixed_fee: int | None
    hourly_rate: int | None


@dataclass(frozen=True, slots=True)
class ChargeRuleRow:
    id: uuid.UUID
    level: str
    service_id: uuid.UUID | None
    rate: int
    effective_from: date
    effective_to: date | None


class JobRateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def client_jobs(
        self, organization_id: uuid.UUID, client_id: uuid.UUID
    ) -> list[JobTermsRow]:
        """The client's live jobs, newest first."""
        stmt = (
            select(Job.id, Job.service_id, Job.billing_basis, Job.fixed_fee, Job.hourly_rate)
            .where(
                Job.organization_id == organization_id,
                Job.client_id == client_id,
                Job.deleted_at.is_(None),
            )
            .order_by(Job.created_at.desc(), Job.id)
        )
        return [JobTermsRow(*row) for row in (await self.session.execute(stmt)).all()]

    async def charge_rules_on(
        self,
        organization_id: uuid.UUID,
        day: date,
        client_id: uuid.UUID,
        service_ids: Collection[uuid.UUID],
    ) -> Sequence[ChargeRuleRow]:
        """The charge-out rules in force on ``day`` that can price the client's jobs: the client's
        own (every client level) and the rates set against the services of its jobs (service +
        grade, staff + service)."""
        scope = [RateRule.client_id == client_id]
        if service_ids:
            scope.append(
                and_(
                    RateRule.client_id.is_(None),
                    RateRule.level.in_(TASK_LEVELS),
                    RateRule.service_id.in_(list(service_ids)),
                )
            )
        stmt = (
            select(
                RateRule.id,
                RateRule.level,
                RateRule.service_id,
                RateRule.rate,
                RateRule.effective_from,
                RateRule.effective_to,
            )
            .where(
                RateRule.organization_id == organization_id,
                RateRule.kind == "charge",
                RateRule.effective_from <= day,
                or_(RateRule.effective_to.is_(None), RateRule.effective_to >= day),
                or_(*scope),
            )
            .order_by(RateRule.effective_from, RateRule.id)
        )
        return [ChargeRuleRow(*row) for row in (await self.session.execute(stmt)).all()]

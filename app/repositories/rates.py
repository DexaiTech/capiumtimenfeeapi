"""Queries behind the V1 rate endpoints (a person's staff rates, a client's rates), on top of the
dated rate rules. Every method is scoped to one organization."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrganizationMember, RateRule, User

# V1 client rates carried no dates: they apply to every day.
RATE_EPOCH = date(2000, 1, 1)
CLIENT_LEVELS = ("client_staff", "client")


@dataclass(frozen=True, slots=True)
class StaffRateRow:
    """One row of a person's V1 rate history: a charge rule and the cost rate that goes with it."""

    id: uuid.UUID
    user_id: uuid.UUID
    effective_from: date
    effective_to: date | None
    charge_rate: int
    cost_rate: int
    created_by: uuid.UUID | None
    created_at: datetime


class StaffRateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_member(
        self, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[StaffRateRow]:
        """The person's charge rules, newest first, each with the cost rule in force on its first
        day (0 when there is none)."""
        stmt = (
            select(RateRule)
            .where(
                RateRule.organization_id == organization_id,
                RateRule.level == "staff",
                RateRule.user_id == user_id,
            )
            .order_by(RateRule.effective_from.desc())
            .execution_options(populate_existing=True)
        )
        rules = list((await self.session.execute(stmt)).scalars())
        costs = [r for r in rules if r.kind == "cost"]
        rows = []
        for rule in (r for r in rules if r.kind == "charge"):
            cost = next((c for c in costs if _covers(c, rule.effective_from)), None)
            rows.append(
                StaffRateRow(
                    id=rule.id,
                    user_id=user_id,
                    effective_from=rule.effective_from,
                    effective_to=rule.effective_to,
                    charge_rate=rule.rate,
                    cost_rate=cost.rate if cost else 0,
                    created_by=rule.created_by,
                    created_at=rule.created_at,
                )
            )
        return rows

    async def get_charge_rule(
        self, organization_id: uuid.UUID, rule_id: uuid.UUID
    ) -> RateRule | None:
        stmt = select(RateRule).where(
            RateRule.organization_id == organization_id,
            RateRule.id == rule_id,
            RateRule.kind == "charge",
            RateRule.level == "staff",
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def cost_rate_in_force(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, day: date
    ) -> int:
        stmt = select(RateRule.rate).where(
            RateRule.organization_id == organization_id,
            RateRule.kind == "cost",
            RateRule.level == "staff",
            RateRule.user_id == user_id,
            RateRule.effective_from <= day,
            (RateRule.effective_to.is_(None)) | (RateRule.effective_to >= day),
        )
        return int((await self.session.execute(stmt)).scalar_one_or_none() or 0)

    async def active_members(
        self, organization_id: uuid.UUID
    ) -> list[tuple[OrganizationMember, User]]:
        """Active members by name."""
        stmt = (
            select(OrganizationMember, User)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.status == "active",
            )
            .order_by(func.lower(User.full_name), OrganizationMember.user_id)
        )
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]


def _covers(rule: RateRule, day: date) -> bool:
    return rule.effective_from <= day and (rule.effective_to is None or day <= rule.effective_to)


class ClientRateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_client(
        self, organization_id: uuid.UUID, client_id: uuid.UUID
    ) -> list[tuple[RateRule, str | None]]:
        """The client's V1-shaped rates (for a person or for everyone) with the person's name:
        the everyone-rate first, then per person by name, newest period first."""
        stmt = (
            select(RateRule, User.full_name)
            .outerjoin(User, User.id == RateRule.user_id)
            .where(
                RateRule.organization_id == organization_id,
                RateRule.client_id == client_id,
                RateRule.kind == "charge",
                RateRule.level.in_(CLIENT_LEVELS),
            )
            .order_by(
                RateRule.user_id.is_not(None),
                func.lower(User.full_name),
                RateRule.effective_from.desc(),
                RateRule.id,
            )
            .execution_options(populate_existing=True)
        )
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def get(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, rule_id: uuid.UUID
    ) -> RateRule | None:
        stmt = select(RateRule).where(
            RateRule.organization_id == organization_id,
            RateRule.client_id == client_id,
            RateRule.id == rule_id,
            RateRule.kind == "charge",
            RateRule.level.in_(CLIENT_LEVELS),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def user_name(self, user_id: uuid.UUID | None) -> str | None:
        if user_id is None:
            return None
        return await self.session.scalar(select(User.full_name).where(User.id == user_id))

"""Charge-out and cost rates, the V1 way: a person's staff rates and a client's rates.

These are the original endpoints, kept working on top of the dated rate rules
(``app/services/rate_rules.py``):

* A staff rate row is a ``staff``-level charge rule plus a cost rule that starts the same day.
  Adding one keeps V1's timeline: it ends the rate that was in force the day before it and runs
  until the person's next rate starts.
* A client rate is a ``client_staff`` (one person) or ``client`` (everyone) charge rule with no
  end, from 2000-01-01 unless a start date is given.
* Used rates cannot be deleted any more (409 ``RATE_IN_USE``); changing a rate never rewrites
  existing entries.
"""

import uuid
from datetime import date, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models import Client, OrganizationMember, RateRule, User
from app.db.models.organization import GONE_STATUSES
from app.db.session import unit_of_work
from app.repositories.clients import ClientRepository
from app.repositories.members import MemberRepository
from app.repositories.rate_rules import RateRuleRepository, Scope
from app.repositories.rates import (
    RATE_EPOCH,
    ClientRateRepository,
    StaffRateRepository,
    StaffRateRow,
)
from app.schemas.rate_rules import RateRuleUpdate
from app.schemas.rates import ClientRateCreate, ClientRateUpdate, StaffRateCreate
from app.services import audit, policy
from app.services.policy import OrgContext
from app.services.rate_rules import RateRuleService, is_exclusion_violation
from app.utils.practice_dates import today_in


async def rate_in_force(
    session: AsyncSession, organization_id: uuid.UUID, user_id: uuid.UUID, on_date: date
) -> tuple[int, int] | None:
    """``(charge_rate, cost_rate)`` of the person's own rates in force on ``on_date``, or None
    when they have no charge-out rate of their own by then (cost is 0 without a cost rate)."""
    rules = RateRuleRepository(session)
    scope = Scope(user_id=user_id)
    charge = await rules.in_force(organization_id, "charge", scope, on_date)
    if charge is None:
        return None
    cost = await rules.in_force(organization_id, "cost", scope, on_date)
    return charge.rate, cost.rate if cost else 0


async def current_rates(
    session: AsyncSession, organization_id: uuid.UUID, on_date: date
) -> dict[uuid.UUID, tuple[int, int]]:
    """``{user_id: (charge_rate, cost_rate)}`` in force on ``on_date`` for everyone with one."""
    found = await RateRuleRepository(session).staff_rules_in_force(organization_id, on_date)
    rates: dict[uuid.UUID, tuple[int, int]] = {}
    for (user_id, kind), rule in found.items():
        if kind == "charge":
            cost = found.get((user_id, "cost"))
            rates[user_id] = (rule.rate, cost.rate if cost else 0)
    return rates


def practice_today(ctx: OrgContext) -> date:
    return today_in(ctx.organization.timezone)


class RateService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.core = RateRuleService(session)
        self.rules = RateRuleRepository(session)
        self.staff_rates = StaffRateRepository(session)
        self.client_rates = ClientRateRepository(session)
        self.members = MemberRepository(session)
        self.clients = ClientRepository(session)

    async def _audit(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        target_type: str,
        target_id: uuid.UUID,
        meta: dict[str, object],
    ) -> None:
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=ctx.organization_id,
            target_type=target_type,
            target_id=target_id,
            meta=meta,
        )

    # --- Staff rates --------------------------------------------------------------------

    @staticmethod
    def can_see_costs(ctx: OrgContext) -> bool:
        """Cost rates are shown only with ``rate.read`` (``rate.manage`` does not imply it)."""
        return policy.has_permission(ctx, "rate.read")

    async def _member(self, ctx: OrgContext, user_id: uuid.UUID) -> OrganizationMember:
        member = await self.members.get(ctx.organization_id, user_id)
        if member is None:
            raise NotFoundError("Member not found.")
        return member

    async def list_staff_rates(self, ctx: OrgContext, user_id: uuid.UUID) -> list[StaffRateRow]:
        await self._member(ctx, user_id)
        return await self.staff_rates.list_for_member(ctx.organization_id, user_id)

    async def _timeline_insert(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        kind: str,
        scope: Scope,
        rate: int,
        start: date,
        source: str,
    ) -> RateRule:
        """Insert a rule that starts on ``start`` and runs until the scope's next rule starts,
        ending the rule that was in force before it."""
        following = await self.rules.following(ctx.organization_id, kind, scope, start)
        end = following.effective_from - timedelta(days=1) if following else None
        return await self.core.write_rule(
            ctx,
            rctx,
            kind=kind,
            scope=scope,
            rate=rate,
            start=start,
            end=end,
            close_previous=True,
            audit_action=None,
            source=source,
        )

    async def create_staff_rate(
        self, ctx: OrgContext, user_id: uuid.UUID, data: StaffRateCreate, rctx: RequestContext
    ) -> StaffRateRow:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            member = await self._member(ctx, user_id)
            if member.status in GONE_STATUSES:
                raise ConflictError("This person has left the practice.", code="MEMBER_REMOVED")
            scope = Scope(user_id=user_id)
            if await self.rules.same_start(org_id, "charge", scope, data.effective_from):
                message = "This person already has a rate starting on this date."
                raise ConflictError(
                    message,
                    code="STAFF_RATE_EXISTS",
                    details=[{"field": "effective_from", "code": "duplicate", "message": message}],
                )
            try:
                charge = await self._timeline_insert(
                    ctx, rctx, "charge", scope, data.charge_rate, data.effective_from, "staff_rates"
                )
                await self._timeline_insert(
                    ctx, rctx, "cost", scope, data.cost_rate, data.effective_from, "staff_rates"
                )
            except ConflictError as exc:
                if exc.code == "RATE_OVERLAP":
                    raise _staff_rate_exists() from exc
                raise
            await self._audit(
                ctx,
                rctx,
                "staff_rate.created",
                "staff_rate",
                charge.id,
                {
                    "member_name": member.user.full_name,
                    "user_id": str(user_id),
                    "effective_from": charge.effective_from.isoformat(),
                    "charge_rate": data.charge_rate,
                    "cost_rate": data.cost_rate,
                },
            )
            rows = await self.staff_rates.list_for_member(org_id, user_id)
        return next(row for row in rows if row.id == charge.id)

    async def delete_staff_rate(
        self, ctx: OrgContext, rate_id: uuid.UUID, rctx: RequestContext
    ) -> None:
        """Delete one row of a person's rate history (its charge rule and the cost rule that
        starts the same day). 409 ``RATE_IN_USE`` once time has been recorded under it."""
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            rule = await self.staff_rates.get_charge_rule(org_id, rate_id)
            if rule is None or rule.user_id is None:
                raise NotFoundError("Staff rate not found.")
            member_name = await self.client_rates.user_name(rule.user_id)
            scope = Scope(user_id=rule.user_id)
            cost = await self.rules.same_start(org_id, "cost", scope, rule.effective_from)
            cost_rate = cost.rate if cost else 0
            charge_rate, start = rule.rate, rule.effective_from
            await self.core.delete_rule(
                ctx, rctx, rule.id, audit_action=None, source="staff_rates", absorb=True
            )
            if cost is not None:
                await self.core.delete_rule(
                    ctx, rctx, cost.id, audit_action=None, source="staff_rates", absorb=True
                )
            await self._audit(
                ctx,
                rctx,
                "staff_rate.deleted",
                "staff_rate",
                rule.id,
                {
                    "member_name": member_name,
                    "user_id": str(rule.user_id),
                    "effective_from": start.isoformat(),
                    "charge_rate": charge_rate,
                    "cost_rate": cost_rate,
                },
            )

    async def current_staff_rates(
        self, ctx: OrgContext, on_date: date | None
    ) -> tuple[date, list[tuple[OrganizationMember, User, RateRule | None, RateRule | None]]]:
        day = on_date or practice_today(ctx)
        members = await self.staff_rates.active_members(ctx.organization_id)
        in_force = await self.rules.staff_rules_in_force(ctx.organization_id, day)
        return day, [
            (m, u, in_force.get((m.user_id, "charge")), in_force.get((m.user_id, "cost")))
            for m, u in members
        ]

    # --- Client rates -------------------------------------------------------------------

    async def _client(self, ctx: OrgContext, client_id: uuid.UUID, *, lock: bool = False) -> Client:
        found = await self.clients.get(
            ctx.organization_id, client_id, lock="no_key_update" if lock else None
        )
        if found is None:
            raise NotFoundError("Client not found.")
        return found

    async def list_client_rates(
        self, ctx: OrgContext, client_id: uuid.UUID
    ) -> list[tuple[RateRule, str | None]]:
        await self._client(ctx, client_id)
        return await self.client_rates.list_for_client(ctx.organization_id, client_id)

    async def create_client_rate(
        self, ctx: OrgContext, client_id: uuid.UUID, data: ClientRateCreate, rctx: RequestContext
    ) -> tuple[RateRule, str | None]:
        org_id = ctx.organization_id
        try:
            async with unit_of_work(self.session):
                client = await self._client(ctx, client_id, lock=True)
                if data.user_id is not None:
                    member = await self.members.get(org_id, data.user_id)
                    if member is None or member.status in GONE_STATUSES:
                        raise ValidationError.for_field(
                            "user_id", "Choose a member of this practice (or null for everyone)."
                        )
                rule = await self.core.write_rule(
                    ctx,
                    rctx,
                    kind="charge",
                    scope=Scope(client_id=client_id, user_id=data.user_id),
                    rate=data.hourly_rate,
                    start=data.effective_from or RATE_EPOCH,
                    end=data.effective_to,
                    audit_action=None,
                    source="client_rates",
                )
                name = await self.client_rates.user_name(rule.user_id)
                await self._audit(
                    ctx,
                    rctx,
                    "client_rate.created",
                    "client_rate",
                    rule.id,
                    {
                        "client_id": str(client_id),
                        "client_code": client.code,
                        "client_name": client.name,
                        "user_id": str(rule.user_id) if rule.user_id else None,
                        "member_name": name,
                        "hourly_rate": rule.rate,
                    },
                )
        except ConflictError as exc:
            if exc.code == "RATE_OVERLAP":
                raise _client_rate_exists(data.user_id) from exc
            raise
        except IntegrityError as exc:
            if is_exclusion_violation(exc):
                raise _client_rate_exists(data.user_id) from exc
            raise
        return rule, name

    async def update_client_rate(
        self,
        ctx: OrgContext,
        client_id: uuid.UUID,
        rate_id: uuid.UUID,
        data: ClientRateUpdate,
        rctx: RequestContext,
    ) -> tuple[RateRule, str | None]:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            client = await self._client(ctx, client_id)
            rule = await self.client_rates.get(org_id, client_id, rate_id)
            if rule is None:
                raise NotFoundError("Client rate not found.")
            old = rule.rate
            view, changes = await self.core.update_rule(
                ctx,
                rctx,
                rule.id,
                RateRuleUpdate(rate=data.hourly_rate),
                audit_action=None,
                source="client_rates",
            )
            rule = view.rule
            name = await self.client_rates.user_name(rule.user_id)
            if changes:
                await self._audit(
                    ctx,
                    rctx,
                    "client_rate.updated",
                    "client_rate",
                    rule.id,
                    {
                        "client_id": str(client_id),
                        "client_code": client.code,
                        "client_name": client.name,
                        "user_id": str(rule.user_id) if rule.user_id else None,
                        "member_name": name,
                        "changes": {"hourly_rate": [old, rule.rate]},
                    },
                )
        return rule, name

    async def delete_client_rate(
        self, ctx: OrgContext, client_id: uuid.UUID, rate_id: uuid.UUID, rctx: RequestContext
    ) -> None:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            client = await self._client(ctx, client_id)
            rule = await self.client_rates.get(org_id, client_id, rate_id)
            if rule is None:
                raise NotFoundError("Client rate not found.")
            name = await self.client_rates.user_name(rule.user_id)
            await self.core.delete_rule(
                ctx, rctx, rule.id, audit_action=None, source="client_rates"
            )
            await self._audit(
                ctx,
                rctx,
                "client_rate.deleted",
                "client_rate",
                rule.id,
                {
                    "client_id": str(client_id),
                    "client_code": client.code,
                    "client_name": client.name,
                    "member_name": name,
                    "user_id": str(rule.user_id) if rule.user_id else None,
                    "hourly_rate": rule.rate,
                },
            )


def _staff_rate_exists() -> ConflictError:
    message = "This person already has a rate starting on this date."
    return ConflictError(
        message,
        code="STAFF_RATE_EXISTS",
        details=[{"field": "effective_from", "code": "duplicate", "message": message}],
    )


def _client_rate_exists(user_id: uuid.UUID | None) -> ConflictError:
    message = (
        "This client already has a rate for everyone."
        if user_id is None
        else "This client already has a rate for this person."
    )
    return ConflictError(
        message,
        code="CLIENT_RATE_EXISTS",
        details=[{"field": "user_id", "code": "duplicate", "message": message}],
    )

"""Filling in the cost of time recorded without one (docs/domain.md, "Rates").

Time saved while nothing gave its person a cost rate has a cost of 0. When a cost rate appears for
them afterwards (their own, or their grade's), their unbilled time that still has no cost takes
it from the cost ladder (``app/services/charge_rates.py``), so the actual cost on the billing
summary, the job budget, WIP and the reports is not left at nothing. Cost does not depend on the
client, the job or how the job is billed.

Only a missing cost is filled in: the charge-out rate and value never change here (re-rating does
that), an entry that already has a cost keeps it, and billed, in-draft, written-off and
period-locked time is left alone. Each call that fills anything writes one audit entry.
"""

import uuid
from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.db.models import OrganizationMember, TimeEntry
from app.services import audit, charge_rates, period_locks
from app.services.charge_rates import RateQuery
from app.services.job_budget_alerts import refresh_job_budgets
from app.services.policy import OrgContext
from app.utils.money import time_value

COSTED_ACTION = "time_entries.costed"


async def grade_members(
    session: AsyncSession, organization_id: uuid.UUID, grade_id: uuid.UUID
) -> list[uuid.UUID]:
    """The people whose current grade is ``grade_id`` (the grade's cost applies to them)."""
    stmt = select(OrganizationMember.user_id).where(
        OrganizationMember.organization_id == organization_id,
        OrganizationMember.grade_id == grade_id,
    )
    return list((await session.execute(stmt)).scalars())


async def fill_missing_costs(
    session: AsyncSession,
    ctx: OrgContext,
    rctx: RequestContext | None,
    user_ids: Collection[uuid.UUID],
    *,
    target_type: str,
    target_id: uuid.UUID,
) -> int:
    """Give the unbilled, unlocked time of ``user_ids`` that has no cost the cost rate the ladder
    gives it now, inside the caller's unit of work. Returns how many entries were filled in."""
    if not user_ids:
        return 0
    org = ctx.organization
    stmt = (
        select(TimeEntry)
        .where(
            TimeEntry.organization_id == org.id,
            TimeEntry.user_id.in_(sorted(set(user_ids))),
            TimeEntry.billing_status == "unbilled",
            TimeEntry.cost_rate == 0,
        )
        .order_by(TimeEntry.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    entries = list((await session.execute(stmt)).scalars())
    if not entries:
        return 0
    locked = await period_locks.locked_dates(session, org.id, {e.entry_date for e in entries})
    open_entries = [e for e in entries if e.entry_date not in locked]
    rates = await charge_rates.resolve_many(
        session,
        org,
        [
            RateQuery(e.user_id, e.client_id, e.job_id, e.entry_date, overtime=e.is_overtime)
            for e in open_entries
        ],
    )
    filled: list[TimeEntry] = []
    for entry, rate in zip(open_entries, rates, strict=True):
        if rate.cost_rate > 0:
            entry.cost_rate = rate.cost_rate
            entry.cost_value = time_value(entry.minutes, rate.cost_rate)
            filled.append(entry)
    if not filled:
        return 0
    await session.flush()
    await audit.record(
        session,
        COSTED_ACTION,
        ctx=rctx,
        actor_user_id=ctx.user.id,
        organization_id=org.id,
        target_type=target_type,
        target_id=target_id,
        meta={
            "entries": len(filled),
            "cost_value": sum(e.cost_value for e in filled),
            "user_ids": sorted({str(e.user_id) for e in filled}),
        },
    )
    await refresh_job_budgets(
        session, org.id, {e.job_id for e in filled}, actor_user_id=ctx.user.id, rctx=rctx
    )
    return len(filled)

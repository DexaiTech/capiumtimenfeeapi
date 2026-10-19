"""Direct-insert factories for the Time & Fees domain.

They write rows straight into the database, bypassing the services, so a test of one
module (billing, reports) can set up data owned by another (clients, time) quickly and
in any state. Values are computed the same way the services do (``app.utils.money``).
Use the API in tests of the module that owns the data.
"""

import itertools
import uuid
from datetime import date, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select

from app.db.models import (
    ActivityCode,
    Client,
    Disbursement,
    Job,
    RateRule,
    Service,
    TimeEntry,
    Timesheet,
    VatRate,
)
from app.db.session import get_sessionmaker
from app.utils.money import time_value
from tests.factories import Actor, add_member, create_user_with_org

_counter = itertools.count(1)


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def practice(
    client: AsyncClient, name: str = "Acme Accountants", **body: Any
) -> tuple[Actor, dict]:
    """A practice (seeded with the default VAT rates, codes, services and sequences) and its
    Owner. Returns the owner and the ``Organization`` object from the API."""
    return await create_user_with_org(client, name, **body)


async def staff(org: dict, role: str = "staff", **kwargs: Any) -> Actor:
    return await add_member(org["id"], role, **kwargs)


async def vat_rate_id(org: dict, code: str = "standard") -> uuid.UUID:
    async with get_sessionmaker()() as session:
        found = await session.scalar(
            select(VatRate.id).where(
                VatRate.organization_id == _uuid(org["id"]),
                VatRate.code == code,
                VatRate.archived_at.is_(None),
            )
        )
    assert found is not None, f"no VAT rate {code}"
    return found


async def activity_code_id(org: dict, code: str = "ACC") -> uuid.UUID:
    async with get_sessionmaker()() as session:
        found = await session.scalar(
            select(ActivityCode.id).where(
                ActivityCode.organization_id == _uuid(org["id"]), ActivityCode.code == code
            )
        )
    assert found is not None, f"no activity code {code}"
    return found


async def service_id(org: dict, code: str = "ACCS") -> uuid.UUID:
    async with get_sessionmaker()() as session:
        found = await session.scalar(
            select(Service.id).where(
                Service.organization_id == _uuid(org["id"]), Service.code == code
            )
        )
    assert found is not None, f"no service {code}"
    return found


async def make_client(org: dict, name: str | None = None, **fields: Any) -> Client:
    number = next(_counter)
    row = Client(
        organization_id=_uuid(org["id"]),
        code=fields.pop("code", f"T{number:04d}"),
        name=name or f"Client {number:03d} Ltd",
        **fields,
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def make_job(
    org: dict,
    client: Client,
    name: str | None = None,
    *,
    billing_basis: str = "hourly",
    fixed_fee: int | None = None,
    **fields: Any,
) -> Job:
    number = next(_counter)
    row = Job(
        organization_id=_uuid(org["id"]),
        client_id=client.id,
        number=fields.pop("number", f"TJ{number:05d}"),
        name=name or f"Job {number:03d}",
        billing_basis=billing_basis,
        fixed_fee=fixed_fee,
        status=fields.pop("status", "in_progress"),
        **fields,
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def _timeline_rule(
    session: Any, org_id: uuid.UUID, user_id: uuid.UUID, kind: str, rate: int, start: date
) -> RateRule:
    """A staff-level rule from ``start`` that runs until the person's next rule of the kind
    starts; the rule before it ends the day before (how a rate history reads)."""
    existing = list(
        (
            await session.execute(
                select(RateRule)
                .where(
                    RateRule.organization_id == org_id,
                    RateRule.kind == kind,
                    RateRule.level == "staff",
                    RateRule.user_id == user_id,
                )
                .order_by(RateRule.effective_from)
            )
        ).scalars()
    )
    previous = [r for r in existing if r.effective_from < start]
    following = [r for r in existing if r.effective_from > start]
    if previous and (previous[-1].effective_to is None or previous[-1].effective_to >= start):
        previous[-1].effective_to = start - timedelta(days=1)
        await session.flush()
    rule = RateRule(
        organization_id=org_id,
        kind=kind,
        level="staff",
        user_id=user_id,
        rate=rate,
        effective_from=start,
        effective_to=following[0].effective_from - timedelta(days=1) if following else None,
    )
    session.add(rule)
    await session.flush()
    return rule


async def make_staff_rate(
    org: dict,
    user: Actor,
    charge_rate: int,
    cost_rate: int = 0,
    effective_from: date = date(2000, 1, 1),
) -> RateRule:
    """A person's rate from ``effective_from``: a staff-level charge rule and cost rule.

    Like the V1 rate history it ends the rate in force before it and runs until the next one.
    Returns the charge rule (its id is the rate's id in the V1 staff-rate endpoints).
    """
    org_id = _uuid(org["id"])
    async with get_sessionmaker()() as session:
        charge = await _timeline_rule(
            session, org_id, user.id, "charge", charge_rate, effective_from
        )
        await _timeline_rule(session, org_id, user.id, "cost", cost_rate, effective_from)
        await session.commit()
        await session.refresh(charge)
    return charge


def week_start_of(day: date, week_start: int = 1) -> date:
    """The ``week_start`` ISO weekday (1 = Monday) on or before ``day``."""
    return day - timedelta(days=(day.isoweekday() - week_start) % 7)


async def make_timesheet(
    org: dict, user: Actor, week_start: date, status: str = "approved"
) -> Timesheet:
    async with get_sessionmaker()() as session:
        found = await session.scalar(
            select(Timesheet).where(
                Timesheet.organization_id == _uuid(org["id"]),
                Timesheet.user_id == user.id,
                Timesheet.week_start == week_start,
            )
        )
        if found is None:
            found = Timesheet(
                organization_id=_uuid(org["id"]),
                user_id=user.id,
                week_start=week_start,
                status=status,
            )
            session.add(found)
        else:
            found.status = status
        await session.commit()
        await session.refresh(found)
    return found


async def make_time_entry(
    org: dict,
    user: Actor,
    *,
    client: Client | None = None,
    job: Job | None = None,
    entry_date: date = date(2026, 9, 7),
    minutes: int = 60,
    charge_rate: int = 12_000,
    cost_rate: int = 4_000,
    is_billable: bool | None = None,
    timesheet_status: str = "approved",
    billing_status: str = "unbilled",
    **fields: Any,
) -> TimeEntry:
    """A time entry on the person's timesheet for that week (created in ``timesheet_status``).
    Values are ``minutes x rate / 60`` like the service computes them."""
    if job is not None and client is None:
        raise ValueError("pass the job's client too")
    sheet = await make_timesheet(org, user, week_start_of(entry_date), timesheet_status)
    billable = (client is not None) if is_billable is None else is_billable
    row = TimeEntry(
        organization_id=_uuid(org["id"]),
        user_id=user.id,
        timesheet_id=sheet.id,
        entry_date=entry_date,
        minutes=minutes,
        client_id=client.id if client else None,
        job_id=job.id if job else None,
        is_billable=billable,
        charge_rate=charge_rate,
        cost_rate=cost_rate,
        charge_value=time_value(minutes, charge_rate),
        cost_value=time_value(minutes, cost_rate),
        billing_status=billing_status,
        **fields,
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def make_disbursement(
    org: dict,
    client: Client,
    amount: int = 1_300,
    *,
    job: Job | None = None,
    incurred_on: date = date(2026, 9, 7),
    description: str = "Companies House filing fee",
    billing_status: str = "unbilled",
    **fields: Any,
) -> Disbursement:
    row = Disbursement(
        organization_id=_uuid(org["id"]),
        client_id=client.id,
        job_id=job.id if job else None,
        incurred_on=incurred_on,
        description=description,
        amount=amount,
        billing_status=billing_status,
        **fields,
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row

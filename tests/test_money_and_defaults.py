"""Money arithmetic, gapless sequences, practice defaults and the domain factories."""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.db.models import ActivityCode, DocumentSequence, Service, VatRate
from app.db.session import get_sessionmaker
from app.services.sequences import next_number
from app.utils.money import apportion, line_net, percentage_bp, round_half_up, time_value, vat_on
from tests.domain_factories import make_client, make_job, make_time_entry, practice


def test_rounding_is_half_up() -> None:
    assert round_half_up(Decimal("2.5")) == 3
    assert round_half_up(Decimal("-2.5")) == -3
    assert round_half_up(Decimal("2.49")) == 2


def test_time_value_and_vat() -> None:
    assert time_value(90, 12_000) == 18_000
    assert time_value(7, 10_000) == 1_167  # 1166.67
    assert time_value(1, 100) == 2  # 1.666...
    assert vat_on(18_000, 2000) == 3_600
    assert vat_on(1_167, 2000) == 233  # 233.4
    assert vat_on(1_175, 2000) == 235
    assert line_net(Decimal("1.50"), 12_000) == 18_000


def test_percentage_bp() -> None:
    assert percentage_bp(3, 4) == 7500
    assert percentage_bp(1, 3) == 3333
    assert percentage_bp(5, 0) is None


@pytest.mark.parametrize(
    ("total", "weights", "expected"),
    [
        (100, [1, 1, 1], [34, 33, 33]),
        (1_000, [600, 300, 100], [600, 300, 100]),
        (999, [600, 300, 100], [599, 300, 100]),
        (0, [5, 5], [0, 0]),
        (10, [0, 0, 0], [4, 3, 3]),
        (-100, [1, 1, 1], [-34, -33, -33]),
        (0, [], []),
    ],
)
def test_apportion_sums_exactly(total: int, weights: list[int], expected: list[int]) -> None:
    parts = apportion(total, weights)
    assert parts == expected
    assert sum(parts) == total


def test_apportion_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="no items"):
        apportion(5, [])
    with pytest.raises(ValueError, match="negative"):
        apportion(5, [1, -1])


async def test_a_new_practice_is_seeded_with_uk_defaults(client: AsyncClient) -> None:
    _, org = await practice(client)
    org_id = uuid.UUID(org["id"])
    assert org["currency"] == "GBP"
    async with get_sessionmaker()() as session:
        rates = {
            r.code: (r.rate_bp, r.is_default)
            for r in await session.scalars(select(VatRate).where(VatRate.organization_id == org_id))
        }
        assert rates["standard"] == (2000, True)
        assert rates["reduced"] == (500, False)
        codes = await session.scalar(
            select(func.count()).where(ActivityCode.organization_id == org_id)
        )
        services = await session.scalar(
            select(func.count()).where(Service.organization_id == org_id)
        )
        kinds = set(
            await session.scalars(
                select(DocumentSequence.kind).where(DocumentSequence.organization_id == org_id)
            )
        )
    assert codes == 11
    assert services == 8
    assert kinds == {"client", "job", "invoice", "credit_note", "staff", "ticket", "estimate"}


async def test_sequences_are_gapless_and_a_rollback_frees_the_number(client: AsyncClient) -> None:
    _, org = await practice(client)
    org_id = uuid.UUID(org["id"])
    async with get_sessionmaker()() as session:
        assert await next_number(session, org_id, "invoice") == "INV-00001"
        await session.rollback()
        assert await next_number(session, org_id, "invoice") == "INV-00001"
        assert await next_number(session, org_id, "invoice") == "INV-00002"
        await session.commit()
        assert await next_number(session, org_id, "credit_note") == "CN-00001"
        await session.commit()


async def test_domain_factories_build_wip(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    job = await make_job(org, acme, "Accounts 2026")
    entry = await make_time_entry(org, owner, client=acme, job=job, minutes=90)
    assert entry.charge_value == 18_000
    assert entry.is_billable
    assert entry.billing_status == "unbilled"

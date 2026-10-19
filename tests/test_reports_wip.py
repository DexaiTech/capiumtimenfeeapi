"""WIP and aged debtors reports: exact figures, buckets at their boundaries, as-at dates."""

from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient

from tests.domain_factories import (
    make_client,
    make_disbursement,
    make_job,
    make_time_entry,
    practice,
    staff,
)
from tests.reports_helpers import (
    LineSpec,
    allocate,
    credit_note,
    make_invoice,
    payment,
    pin_now,
    report,
    set_org,
    update_row,
    write_off,
)

AS_AT = date(2026, 10, 17)


def ago(days: int) -> date:
    return AS_AT - timedelta(days=days)


async def _wip_dataset(client: AsyncClient):
    owner, org = await practice(client)
    member = await staff(org, "staff", full_name="Sam Staff")
    partner = await staff(org, "partner", full_name="Pat Partner")
    alpha = await make_client(org, "Alpha Ltd", partner_user_id=owner.id)
    beta = await make_client(org, "Beta LLP", partner_user_id=partner.id)
    a1 = await make_job(org, alpha, "Accounts 2026", number="J00001", partner_user_id=partner.id)
    b1 = await make_job(org, beta, "VAT Q3", number="J00002")
    # Ages 0, 30 | 31, 60 | 61, 90 | 91 days: both sides of every boundary.
    for days, minutes, rate in (
        (0, 60, 12_000),
        (30, 90, 12_000),
        (31, 7, 10_000),  # 1166.67 -> 1167
        (60, 30, 12_000),
        (61, 15, 12_000),
        (90, 60, 12_000),
        (91, 60, 12_000),
    ):
        await make_time_entry(
            org,
            owner,
            client=alpha,
            job=a1,
            entry_date=ago(days),
            minutes=minutes,
            charge_rate=rate,
        )
    # Not WIP: on a submitted timesheet, billed, in a draft, non-billable, after the date.
    await make_time_entry(
        org, member, client=alpha, job=a1, entry_date=ago(2), timesheet_status="submitted"
    )
    await make_time_entry(
        org, owner, client=alpha, job=a1, entry_date=ago(3), billing_status="billed"
    )
    await make_time_entry(
        org, owner, client=alpha, job=a1, entry_date=ago(3), billing_status="in_draft"
    )
    await make_time_entry(org, owner, client=alpha, job=a1, entry_date=ago(4), is_billable=False)
    await make_time_entry(org, owner, client=alpha, job=a1, entry_date=AS_AT + timedelta(days=1))
    await make_disbursement(org, alpha, 1_300, job=a1, incurred_on=ago(45))
    await make_disbursement(org, alpha, 2_500, incurred_on=ago(5))
    await make_disbursement(org, alpha, 999, incurred_on=ago(5), is_billable=False)
    await make_time_entry(
        org, member, client=beta, job=b1, entry_date=ago(10), minutes=60, charge_rate=9_000
    )
    return owner, org, member, partner, alpha, beta, a1, b1


async def test_wip_rows_buckets_and_exclusions(client: AsyncClient) -> None:
    owner, org, _, _, alpha, _, a1, b1 = await _wip_dataset(client)
    data = await report(client, owner, org, "wip", as_at=AS_AT)

    assert [(r["client_name"], r["job_number"]) for r in data["rows"]] == [
        ("Alpha Ltd", None),
        ("Alpha Ltd", "J00001"),
        ("Beta LLP", "J00002"),
    ]
    no_job, accounts, vat = data["rows"]
    assert no_job == {
        "client_id": str(alpha.id),
        "client_code": alpha.code,
        "client_name": "Alpha Ltd",
        "job_id": None,
        "job_number": None,
        "job_name": None,
        "oldest_item_date": ago(5).isoformat(),
        "minutes": 0,
        "time_value": 0,
        "disbursement_value": 2_500,
        "total": 2_500,
        "age_0_30": 2_500,
        "age_31_60": 0,
        "age_61_90": 0,
        "age_91_plus": 0,
    }
    assert accounts["job_id"] == str(a1.id)
    assert accounts["minutes"] == 322
    assert accounts["time_value"] == 64_167
    assert accounts["disbursement_value"] == 1_300
    assert accounts["total"] == 65_467
    assert (accounts["age_0_30"], accounts["age_31_60"]) == (30_000, 8_467)
    assert (accounts["age_61_90"], accounts["age_91_plus"]) == (15_000, 12_000)
    assert accounts["oldest_item_date"] == ago(91).isoformat()
    assert vat["job_id"] == str(b1.id)
    assert vat["total"] == 9_000
    assert data["totals"] == {
        "minutes": 382,
        "time_value": 73_167,
        "disbursement_value": 3_800,
        "total": 76_967,
        "age_0_30": 41_500,
        "age_31_60": 8_467,
        "age_61_90": 15_000,
        "age_91_plus": 12_000,
        "oldest_item_date": ago(91).isoformat(),
    }
    assert data["filters"]["as_at"] == AS_AT.isoformat()


async def test_wip_without_approval_includes_unapproved_time(client: AsyncClient) -> None:
    owner, org, *_ = await _wip_dataset(client)
    await set_org(org, require_timesheet_approval=False)
    data = await report(client, owner, org, "wip", as_at=AS_AT)
    assert data["totals"]["total"] == 76_967 + 12_000
    assert data["rows"][1]["age_0_30"] == 30_000 + 12_000


async def test_wip_as_at_an_earlier_day_ages_from_that_day(client: AsyncClient) -> None:
    owner, org, *_ = await _wip_dataset(client)
    data = await report(client, owner, org, "wip", as_at=ago(31))
    # Items dated after the day are left out; ages count from that day.
    assert [r["job_number"] for r in data["rows"]] == ["J00001"]
    accounts = data["rows"][0]
    assert accounts["minutes"] == 7 + 30 + 15 + 60 + 60
    assert accounts["age_0_30"] == 1_167 + 6_000 + 3_000 + 1_300  # ages 0, 29, 30, 14
    assert accounts["age_31_60"] == 12_000 + 12_000  # ages 59, 60
    assert accounts["age_61_90"] == accounts["age_91_plus"] == 0


async def test_wip_as_at_counts_items_cleared_after_that_day(client: AsyncClient) -> None:
    """Month-end WIP run later: items billed or written off after the day were still WIP."""
    owner, org = await practice(client)
    acme = await make_client(org, "Acme")
    month_end = ago(17)  # 2026-09-30
    billed_later = await make_time_entry(org, owner, client=acme, entry_date=ago(20))  # 12000
    billed_before = await make_time_entry(
        org, owner, client=acme, entry_date=ago(25), minutes=30
    )  # 6000
    written_later = await make_disbursement(org, acme, 1_300, incurred_on=ago(22))
    written_before = await make_disbursement(org, acme, 700, incurred_on=ago(22))
    drafted = await make_time_entry(org, owner, client=acme, entry_date=ago(21), minutes=15)
    october = await make_invoice(org, acme, [LineSpec(12_000)], issue_date=ago(5))
    await allocate(org, october, billed_later)
    september = await make_invoice(org, acme, [LineSpec(6_000)], issue_date=ago(18))
    await allocate(org, september, billed_before)
    await write_off(org, acme, [written_later], written_off_on=ago(10))
    await write_off(org, acme, [written_before], written_off_on=ago(17))
    draft = await make_invoice(org, acme, [LineSpec(3_000)], issue_date=None, status="draft")
    await allocate(org, draft, drafted)

    then = await report(client, owner, org, "wip", as_at=month_end)
    assert then["totals"]["time_value"] == 12_000
    assert then["totals"]["disbursement_value"] == 1_300
    assert then["totals"]["total"] == 13_300
    # As at today all of it is cleared (the draft's item is in_draft, not WIP).
    assert (await report(client, owner, org, "wip", as_at=AS_AT))["totals"]["total"] == 0


async def test_wip_filters(client: AsyncClient) -> None:
    owner, org, member, partner, _, beta, a1, _ = await _wip_dataset(client)

    by_client = await report(client, owner, org, "wip", as_at=AS_AT, client_id=beta.id)
    assert [r["client_name"] for r in by_client["rows"]] == ["Beta LLP"]
    assert by_client["filters"]["client_id"] == str(beta.id)

    by_job = await report(client, owner, org, "wip", as_at=AS_AT, job_id=a1.id)
    assert [r["job_number"] for r in by_job["rows"]] == ["J00001"]

    # A person's time only: disbursements are left out.
    by_user = await report(client, owner, org, "wip", as_at=AS_AT, user_id=member.id)
    assert [(r["client_name"], r["total"]) for r in by_user["rows"]] == [("Beta LLP", 9_000)]

    # The job's partner wins over the client's; a job without one falls back to the client.
    by_partner = await report(client, owner, org, "wip", as_at=AS_AT, partner_user_id=partner.id)
    assert [(r["client_name"], r["job_number"]) for r in by_partner["rows"]] == [
        ("Alpha Ltd", "J00001"),
        ("Beta LLP", "J00002"),
    ]
    by_owner = await report(client, owner, org, "wip", as_at=AS_AT, partner_user_id=owner.id)
    assert [(r["client_name"], r["job_number"]) for r in by_owner["rows"]] == [("Alpha Ltd", None)]


async def test_wip_service_and_manager_filters(client: AsyncClient) -> None:
    from tests.domain_factories import service_id

    owner, org = await practice(client)
    manager = await staff(org, "manager")
    acme = await make_client(org, "Acme", manager_user_id=manager.id)
    accounts = await make_job(org, acme, "Accounts", service_id=await service_id(org, "ACCS"))
    payroll = await make_job(org, acme, "Payroll")
    await make_time_entry(org, owner, client=acme, job=accounts, entry_date=ago(1))
    await make_time_entry(org, owner, client=acme, job=payroll, entry_date=ago(1), minutes=30)

    by_service = await report(
        client, owner, org, "wip", as_at=AS_AT, service_id=await service_id(org, "ACCS")
    )
    assert [r["job_name"] for r in by_service["rows"]] == ["Accounts"]
    by_manager = await report(client, owner, org, "wip", as_at=AS_AT, manager_user_id=manager.id)
    assert by_manager["totals"]["total"] == 12_000 + 6_000


# --- aged debtors ----------------------------------------------------------------------


async def _debtors_dataset(client: AsyncClient):
    owner, org = await practice(client)
    alpha = await make_client(org, "Alpha Ltd")
    beta = await make_client(org, "Beta LLP")

    async def invoice(c, overdue_days: int, net: int = 1_000, **kw):
        due = ago(overdue_days)
        return await make_invoice(
            org,
            c,
            [LineSpec(net)],
            issue_date=min(due - timedelta(days=30), AS_AT),
            due_date=due,
            **kw,
        )

    await invoice(alpha, 0)  # due today: current
    await make_invoice(
        org, alpha, [LineSpec(500)], issue_date=ago(20), due_date=AS_AT + timedelta(days=10)
    )
    part_paid = await invoice(alpha, 1, net=2_000)  # 1-30
    credited = await invoice(alpha, 30)  # 1-30
    paid_later = await invoice(alpha, 31)  # 31-60
    await invoice(alpha, 60)  # 31-60
    await invoice(alpha, 61)  # 61-90
    await invoice(alpha, 90)  # 61-90
    await invoice(alpha, 91)  # 91+
    fully_paid = await invoice(alpha, 100)
    await invoice(alpha, 40, net=5_000, status="void")
    await make_invoice(org, alpha, [LineSpec(7_000)], issue_date=None, status="draft")
    await make_invoice(org, alpha, [LineSpec(4_000)], issue_date=AS_AT + timedelta(days=1))
    await payment(org, alpha, 400, received_on=ago(2), allocations=[(part_paid, 400)])
    await credit_note(org, credited, [(200, 2000)], issue_date=ago(3))
    await payment(org, alpha, 1_200, received_on=ago(95), allocations=[(fully_paid, 1_200)])
    # Received after the report date: the invoice was still owed as at that date.
    await payment(
        org, alpha, 1_200, received_on=AS_AT + timedelta(days=1), allocations=[(paid_later, 1_200)]
    )
    await invoice(beta, 5, net=2_500)
    return owner, org, alpha, beta


async def test_aged_debtors_buckets_payments_and_credits(client: AsyncClient) -> None:
    owner, org, alpha, beta = await _debtors_dataset(client)
    data = await report(client, owner, org, "aged-debtors", as_at=AS_AT)
    assert [r["client_name"] for r in data["rows"]] == ["Alpha Ltd", "Beta LLP"]
    a, b = data["rows"]
    assert a == {
        "client_id": str(alpha.id),
        "client_code": alpha.code,
        "client_name": "Alpha Ltd",
        "invoice_count": 9,
        "current": 1_200 + 600,
        "days_1_30": (2_400 - 400) + (1_200 - 240),
        "days_31_60": 1_200 + 1_200,
        "days_61_90": 1_200 + 1_200,
        "days_91_plus": 1_200,
        "days_90_plus": 1_200,
        "total": 10_760,
        "oldest_due_date": ago(91).isoformat(),
    }
    assert (b["client_id"], b["days_1_30"], b["total"]) == (str(beta.id), 3_000, 3_000)
    assert data["totals"]["total"] == 13_760
    assert data["totals"]["invoice_count"] == 10
    assert data["totals"]["current"] == 1_800


async def test_aged_debtors_as_at_the_next_day(client: AsyncClient) -> None:
    owner, org, *_ = await _debtors_dataset(client)
    data = await report(client, owner, org, "aged-debtors", as_at=AS_AT + timedelta(days=1))
    a = data["rows"][0]
    # The late payment now counts (that invoice drops out); the invoice issued that day
    # joins as current; every invoice is a day older (0 -> 1, 30 -> 31, 60 -> 61, 90 -> 91).
    assert a["invoice_count"] == 9
    assert a["current"] == 600 + 4_800
    assert a["days_1_30"] == 1_200 + 2_000
    assert a["days_31_60"] == 960
    assert a["days_61_90"] == 1_200 + 1_200
    assert a["days_91_plus"] == 1_200 + 1_200
    assert a["total"] == 5_400 + 3_200 + 960 + 2_400 + 2_400


async def test_aged_debtors_filters(client: AsyncClient) -> None:
    owner, org = await practice(client)
    partner = await staff(org, "partner")
    mine = await make_client(org, "Mine", partner_user_id=partner.id, manager_user_id=owner.id)
    other = await make_client(org, "Other")
    await make_invoice(org, mine, [LineSpec(1_000)], issue_date=ago(10))
    await make_invoice(org, other, [LineSpec(2_000)], issue_date=ago(10))
    for params in (
        {"partner_user_id": partner.id},
        {"manager_user_id": owner.id},
        {"client_id": mine.id},
    ):
        data = await report(client, owner, org, "aged-debtors", as_at=AS_AT, **params)
        assert [r["client_name"] for r in data["rows"]] == ["Mine"]


# --- practice time zone ----------------------------------------------------------------


async def test_default_dates_are_today_in_the_practice_time_zone(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme")
    # 23:30 UTC on the 16th is 00:30 on the 17th in London (BST).
    pin_now(monkeypatch, datetime(2026, 10, 16, 23, 30, tzinfo=UTC))
    await make_invoice(
        org, acme, [LineSpec(1_000)], issue_date=date(2026, 9, 16), due_date=date(2026, 10, 16)
    )
    await make_time_entry(org, owner, client=acme, entry_date=date(2026, 10, 17))

    debtors = await report(client, owner, org, "aged-debtors")
    assert debtors["filters"]["as_at"] == "2026-10-17"
    assert (debtors["totals"]["current"], debtors["totals"]["days_1_30"]) == (0, 1_200)
    wip = await report(client, owner, org, "wip")
    assert wip["filters"]["as_at"] == "2026-10-17"
    assert wip["totals"]["total"] == 12_000

    # An hour earlier it is still the 16th in London: due today, and the entry is tomorrow's.
    pin_now(monkeypatch, datetime(2026, 10, 16, 22, 30, tzinfo=UTC))
    debtors = await report(client, owner, org, "aged-debtors")
    assert debtors["filters"]["as_at"] == "2026-10-16"
    assert (debtors["totals"]["current"], debtors["totals"]["days_1_30"]) == (1_200, 0)
    assert (await report(client, owner, org, "wip"))["totals"]["total"] == 0


async def test_reversed_allocations_put_items_back_in_wip(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme")
    entry = await make_time_entry(org, owner, client=acme, entry_date=ago(3))
    voided = await make_invoice(org, acme, [LineSpec(12_000)], issue_date=ago(2), status="void")
    await allocate(org, voided, entry, reversed_at=datetime.now(UTC))
    data = await report(client, owner, org, "wip", as_at=AS_AT)
    assert data["totals"]["total"] == 12_000
    await update_row(type(entry), entry.id, billing_status="billed")
    assert (await report(client, owner, org, "wip", as_at=AS_AT))["totals"]["total"] == 0

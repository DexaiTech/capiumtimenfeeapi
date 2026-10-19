"""Time summary and utilisation reports."""

from datetime import date

from httpx import AsyncClient

from tests.domain_factories import (
    activity_code_id,
    make_client,
    make_job,
    make_time_entry,
    practice,
    service_id,
    staff,
)
from tests.factories import error
from tests.reports_helpers import fetch, report, set_member, set_org

SEPTEMBER = {"date_from": date(2026, 9, 1), "date_to": date(2026, 9, 30)}


async def _time_dataset(client: AsyncClient):
    owner, org = await practice(client, full_name="Olivia Owner")
    member = await staff(org, "staff", full_name="Sam Staff")
    alpha = await make_client(org, "Alpha Ltd", code="ALPHA")
    beta = await make_client(org, "Beta LLP", code="BETA")
    a1 = await make_job(
        org, alpha, "Accounts 2026", number="J00001", service_id=await service_id(org, "ACCS")
    )
    b1 = await make_job(org, beta, "VAT Q3", number="J00002")
    acc, adm = await activity_code_id(org, "ACC"), await activity_code_id(org, "ADM")
    # Owner, rate 120/h, cost 40/h.
    await make_time_entry(
        org,
        owner,
        client=alpha,
        job=a1,
        entry_date=date(2026, 9, 7),
        minutes=90,
        activity_code_id=acc,
    )
    await make_time_entry(
        org,
        owner,
        entry_date=date(2026, 9, 8),
        minutes=30,
        is_billable=False,
        activity_code_id=adm,
    )
    await make_time_entry(org, owner, client=beta, job=b1, entry_date=date(2026, 9, 13), minutes=45)
    # Staff, rate 60/h, cost 20/h.
    await make_time_entry(
        org,
        member,
        client=alpha,
        job=a1,
        entry_date=date(2026, 9, 14),
        minutes=60,
        charge_rate=6_000,
        cost_rate=2_000,
        activity_code_id=acc,
    )
    # Outside the range.
    await make_time_entry(org, member, client=alpha, job=a1, entry_date=date(2026, 8, 31))
    await make_time_entry(org, member, client=alpha, job=a1, entry_date=date(2026, 10, 1))
    return owner, org, member, alpha, beta, a1, b1


def _measures(row: dict) -> tuple:
    return (
        row["entry_count"],
        row["total_minutes"],
        row["chargeable_minutes"],
        row["non_chargeable_minutes"],
        row["chargeable_bp"],
        row["charge_value"],
        row["cost_value"],
    )


async def test_time_summary_by_person(client: AsyncClient) -> None:
    owner, org, member, *_ = await _time_dataset(client)
    data = await report(client, owner, org, "time-summary", **SEPTEMBER)
    assert [(r["key"], r["name"]) for r in data["rows"]] == [
        (str(owner.id), "Olivia Owner"),
        (str(member.id), "Sam Staff"),
    ]
    # 135 of 165 minutes chargeable = 81.818% -> 8182 bp.
    assert _measures(data["rows"][0]) == (3, 165, 135, 30, 8182, 18_000 + 9_000, 11_000)
    assert _measures(data["rows"][1]) == (1, 60, 60, 0, 10_000, 6_000, 2_000)
    assert data["totals"] == {
        "entry_count": 4,
        "total_minutes": 225,
        "chargeable_minutes": 195,
        "non_chargeable_minutes": 30,
        "chargeable_bp": 8667,
        "charge_value": 33_000,
        "cost_value": 13_000,
    }
    assert data["filters"]["date_from"] == "2026-09-01"
    assert data["filters"]["group_by"] == "person"


async def test_time_summary_by_client_job_activity_service(client: AsyncClient) -> None:
    owner, org, _, alpha, beta, a1, b1 = await _time_dataset(client)

    by_client = await report(client, owner, org, "time-summary", group_by="client", **SEPTEMBER)
    assert [(r["key"], r["code"], r["name"], r["total_minutes"]) for r in by_client["rows"]] == [
        (str(alpha.id), "ALPHA", "Alpha Ltd", 150),
        (str(beta.id), "BETA", "Beta LLP", 45),
        (None, None, "No client (internal time)", 30),
    ]

    by_job = await report(client, owner, org, "time-summary", group_by="job", **SEPTEMBER)
    assert [
        (r["key"], r["code"], r["name"], r["client_name"], r["total_minutes"])
        for r in by_job["rows"]
    ] == [
        (str(a1.id), "J00001", "Accounts 2026", "Alpha Ltd", 150),
        (str(b1.id), "J00002", "VAT Q3", "Beta LLP", 45),
        (None, None, "No job", None, 30),
    ]

    by_activity = await report(client, owner, org, "time-summary", group_by="activity", **SEPTEMBER)
    assert [(r["code"], r["name"], r["total_minutes"]) for r in by_activity["rows"]] == [
        ("ACC", "Accounts preparation", 150),
        ("ADM", "Practice administration", 30),
        (None, "No activity code", 45),
    ]
    assert by_activity["rows"][1]["chargeable_minutes"] == 0

    by_service = await report(client, owner, org, "time-summary", group_by="service", **SEPTEMBER)
    assert [(r["code"], r["total_minutes"]) for r in by_service["rows"]] == [
        ("ACCS", 150),
        (None, 75),
    ]


async def test_time_summary_by_day_week_and_month(client: AsyncClient) -> None:
    owner, org, *_ = await _time_dataset(client)
    by_week = await report(client, owner, org, "time-summary", group_by="week", **SEPTEMBER)
    assert [(r["key"], r["name"], r["total_minutes"]) for r in by_week["rows"]] == [
        ("2026-09-07", "Week of 2026-09-07", 165),
        ("2026-09-14", "Week of 2026-09-14", 60),
    ]
    # Weeks starting on Sunday: Sunday the 13th opens a week with Monday the 14th.
    await set_org(org, week_start=7)
    by_week = await report(client, owner, org, "time-summary", group_by="week", **SEPTEMBER)
    assert [(r["key"], r["total_minutes"]) for r in by_week["rows"]] == [
        ("2026-09-06", 120),
        ("2026-09-13", 105),
    ]

    by_day = await report(client, owner, org, "time-summary", group_by="day", **SEPTEMBER)
    assert [r["key"] for r in by_day["rows"]] == [
        "2026-09-07",
        "2026-09-08",
        "2026-09-13",
        "2026-09-14",
    ]

    by_month = await report(
        client,
        owner,
        org,
        "time-summary",
        group_by="month",
        date_from=date(2026, 8, 1),
        date_to=date(2026, 10, 31),
    )
    assert [(r["key"], r["name"], r["total_minutes"]) for r in by_month["rows"]] == [
        ("2026-08-01", "Aug 2026", 60),
        ("2026-09-01", "Sep 2026", 225),
        ("2026-10-01", "Oct 2026", 60),
    ]


async def test_time_summary_filters(client: AsyncClient) -> None:
    owner, org, member, alpha, _, a1, _ = await _time_dataset(client)
    for params, minutes in (
        ({"client_id": alpha.id}, 150),
        ({"job_id": a1.id}, 150),
        ({"user_id": member.id}, 60),
        ({"activity_code_id": await activity_code_id(org, "ADM")}, 30),
        ({"service_id": await service_id(org, "ACCS")}, 150),
    ):
        data = await report(client, owner, org, "time-summary", **SEPTEMBER, **params)
        assert data["totals"]["total_minutes"] == minutes, params


async def test_time_summary_without_read_all_covers_own_time_and_hides_cost(
    client: AsyncClient,
) -> None:
    owner, org, member, *_ = await _time_dataset(client)
    data = await report(client, member, org, "time-summary", **SEPTEMBER)
    assert [r["key"] for r in data["rows"]] == [str(member.id)]
    assert data["filters"]["user_id"] == str(member.id)
    assert data["rows"][0]["cost_value"] is None
    assert data["totals"]["cost_value"] is None

    response = await fetch(client, member, org, "time-summary", user_id=owner.id, **SEPTEMBER)
    assert response.status_code == 403
    assert error(response)["code"] == "PERMISSION_DENIED"

    # Billing reads everyone's time but not rates.
    billing = await staff(org, "billing")
    data = await report(client, billing, org, "time-summary", **SEPTEMBER)
    assert data["totals"]["total_minutes"] == 225
    assert data["totals"]["cost_value"] is None
    assert data["totals"]["charge_value"] == 33_000


async def test_utilisation(client: AsyncClient) -> None:
    owner, org, member, *_ = await _time_dataset(client)
    await set_member(org, member, weekly_capacity_minutes=1_800, chargeable_target_bp=6_000)
    data = await report(client, owner, org, "utilisation", **SEPTEMBER)
    # September 2026 has 22 weekdays: capacity = weekly x 22 / 5.
    assert data["rows"] == [
        {
            "user_id": str(owner.id),
            "full_name": "Olivia Owner",
            "initials": None,
            "status": "active",
            "capacity_minutes": 9_900,
            "total_minutes": 165,
            "chargeable_minutes": 135,
            "non_chargeable_minutes": 30,
            "utilisation_bp": 136,  # 135 / 9900 = 1.3636%
            "target_bp": 7_500,
            "variance_bp": 136 - 7_500,
        },
        {
            "user_id": str(member.id),
            "full_name": "Sam Staff",
            "initials": None,
            "status": "active",
            "capacity_minutes": 7_920,
            "total_minutes": 60,
            "chargeable_minutes": 60,
            "non_chargeable_minutes": 0,
            "utilisation_bp": 76,  # 60 / 7920 = 0.7576%
            "target_bp": 6_000,
            "variance_bp": 76 - 6_000,
        },
    ]
    assert data["totals"] == {
        "working_days": 22,
        "capacity_minutes": 17_820,
        "total_minutes": 225,
        "chargeable_minutes": 195,
        "non_chargeable_minutes": 30,
        "utilisation_bp": 109,  # 195 / 17820 = 1.0943%
        # (7500 x 9900 + 6000 x 7920) / 17820 = 6833.33
        "target_bp": 6_833,
        "variance_bp": 109 - 6_833,
    }


async def test_utilisation_working_days_members_and_filters(client: AsyncClient) -> None:
    owner, org, member, *_ = await _time_dataset(client)
    removed_idle = await staff(org, "staff", full_name="Gone Idle", status="removed")
    removed_busy = await staff(org, "staff", full_name="Gone Busy", status="removed")
    await make_time_entry(org, removed_busy, entry_date=date(2026, 9, 2), minutes=10)
    await set_org(org, working_days=[1, 2, 3, 4])
    await set_member(org, member, manager_user_id=owner.id)

    data = await report(client, owner, org, "utilisation", **SEPTEMBER)
    names = [r["full_name"] for r in data["rows"]]
    assert names == ["Gone Busy", "Olivia Owner", "Sam Staff"]
    assert removed_idle.id not in [r["user_id"] for r in data["rows"]]
    # Monday-Thursday: 18 working days in September 2026; 2250 x 18 / 4 = 10125.
    assert data["totals"]["working_days"] == 18
    assert data["rows"][1]["capacity_minutes"] == 10_125

    managed = await report(client, owner, org, "utilisation", manager_user_id=owner.id, **SEPTEMBER)
    assert [r["full_name"] for r in managed["rows"]] == ["Sam Staff"]
    one = await report(client, owner, org, "utilisation", user_id=member.id, **SEPTEMBER)
    assert [r["full_name"] for r in one["rows"]] == ["Sam Staff"]

    # Without time.read_all: one's own row only.
    own = await report(client, member, org, "utilisation", **SEPTEMBER)
    assert [r["full_name"] for r in own["rows"]] == ["Sam Staff"]
    assert (await fetch(client, member, org, "utilisation", user_id=owner.id)).status_code == 403


async def test_utilisation_zero_capacity_is_null(client: AsyncClient) -> None:
    owner, org = await practice(client)
    await set_member(org, owner, weekly_capacity_minutes=0)
    data = await report(client, owner, org, "utilisation", **SEPTEMBER)
    row = data["rows"][0]
    assert (row["capacity_minutes"], row["utilisation_bp"], row["variance_bp"]) == (0, None, None)
    assert data["totals"]["utilisation_bp"] is None
    assert data["totals"]["target_bp"] is None

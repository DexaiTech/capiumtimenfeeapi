"""The dashboard: the caller's week, timer, timesheets and jobs, and the practice figures."""

import asyncio
from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from app.db.session import get_engine
from tests.domain_factories import (
    make_client,
    make_disbursement,
    make_job,
    make_time_entry,
    practice,
    staff,
)
from tests.factories import add_member, error
from tests.reports_helpers import (
    LineSpec,
    assign,
    custom_role,
    dashboard_url,
    make_invoice,
    make_timer,
    pin_now,
    set_member,
)

# Saturday 17 October 2026, 00:30 in London (BST); still Friday the 16th in UTC.
NOW = datetime(2026, 10, 16, 23, 30, tzinfo=UTC)


async def _get(client: AsyncClient, actor, org) -> dict:
    response = await client.get(dashboard_url(org), headers=actor.headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _dataset(client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
    pin_now(monkeypatch, NOW)
    owner, org = await practice(client)
    member = await staff(org, "staff", full_name="Sam Staff")
    other = await staff(org, "staff", full_name="Olly Other")
    await set_member(org, member, manager_user_id=owner.id)
    alpha = await make_client(org, "Alpha Ltd")
    beta = await make_client(org, "Beta LLP")
    managed = await make_job(
        org,
        alpha,
        "Accounts",
        number="J00001",
        due_date=date(2026, 10, 20),
        manager_user_id=owner.id,
    )
    assigned = await make_job(org, beta, "VAT", number="J00002", due_date=date(2026, 10, 31))
    await assign(org, assigned, owner)
    late = await make_job(
        org,
        alpha,
        "Late",
        number="J00003",
        due_date=date(2026, 10, 10),
        manager_user_id=owner.id,
        status="on_hold",
    )
    await make_job(
        org, alpha, "Too far", number="J00004", due_date=date(2026, 11, 1), manager_user_id=owner.id
    )
    await make_job(
        org,
        alpha,
        "Done",
        number="J00005",
        due_date=date(2026, 10, 18),
        manager_user_id=owner.id,
        status="completed",
    )
    await make_job(org, alpha, "Someone else's", number="J00006", due_date=date(2026, 10, 18))

    # This week (Mon 12 - Sun 18 October), open timesheet.
    await make_time_entry(
        org,
        owner,
        client=alpha,
        job=managed,
        entry_date=date(2026, 10, 12),
        minutes=90,
        timesheet_status="open",
    )
    await make_time_entry(
        org, owner, entry_date=date(2026, 10, 13), minutes=30, timesheet_status="open"
    )
    # Past weeks: open, rejected (both unsubmitted) and approved.
    await make_time_entry(
        org, owner, client=alpha, job=managed, entry_date=date(2026, 10, 9), timesheet_status="open"
    )
    await make_time_entry(
        org,
        owner,
        client=alpha,
        job=managed,
        entry_date=date(2026, 9, 29),
        minutes=30,
        timesheet_status="rejected",
    )
    await make_time_entry(
        org, owner, client=alpha, job=late, entry_date=date(2026, 7, 1), timesheet_status="approved"
    )
    await make_disbursement(org, beta, 1_300, incurred_on=date(2026, 10, 1))
    # Approvals: one sheet of someone the owner manages, one of someone else.
    await make_time_entry(org, member, entry_date=date(2026, 10, 5), timesheet_status="submitted")
    await make_time_entry(org, other, entry_date=date(2026, 10, 5), timesheet_status="submitted")

    # Debtors: due "yesterday" in London is overdue; due today is not.
    await make_invoice(
        org, alpha, [LineSpec(1_000)], issue_date=date(2026, 10, 1), due_date=date(2026, 10, 16)
    )
    await make_invoice(
        org, beta, [LineSpec(500)], issue_date=date(2026, 9, 16), due_date=date(2026, 10, 17)
    )
    await make_invoice(org, beta, [LineSpec(9_999)], issue_date=date(2026, 10, 2), status="void")
    await make_timer(
        org,
        owner,
        started_at=datetime(2026, 10, 16, 23, 0, tzinfo=UTC),
        accumulated_seconds=600,
        client=alpha,
        job=managed,
        description="Review",
    )
    return owner, org, member, alpha, beta, managed, assigned, late


async def test_dashboard_for_the_owner(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, org, _, alpha, *_ = await _dataset(client, monkeypatch)
    data = await _get(client, owner, org)

    assert data["today"] == "2026-10-17"
    assert data["week"] == {
        "week_start": "2026-10-12",
        "week_end": "2026-10-18",
        "total_minutes": 120,
        "chargeable_minutes": 90,
        "non_chargeable_minutes": 30,
        "capacity_minutes": 2_250,
        "utilisation_bp": 400,
        "target_bp": 7_500,
    }
    timer = data["timer"]
    assert (timer["is_running"], timer["accumulated_seconds"], timer["elapsed_seconds"]) == (
        True,
        600,
        600 + 1_800,
    )
    assert (timer["client_name"], timer["job_number"], timer["description"]) == (
        "Alpha Ltd",
        "J00001",
        "Review",
    )

    assert data["unsubmitted_week_count"] == 2
    assert [
        (w["week_start"], w["status"], w["total_minutes"]) for w in data["unsubmitted_weeks"]
    ] == [
        ("2026-10-05", "open", 60),
        ("2026-09-28", "rejected", 30),
    ]

    jobs = data["jobs_due"]
    assert [(j["number"], j["days_until_due"], j["is_overdue"]) for j in jobs] == [
        ("J00003", -7, True),
        ("J00001", 3, False),
        ("J00002", 14, False),
    ]
    assert (jobs[1]["is_manager"], jobs[1]["is_assignee"]) == (True, False)
    assert (jobs[2]["is_manager"], jobs[2]["is_assignee"]) == (False, True)
    assert jobs[2]["client_name"] == "Beta LLP"

    assert data["approvals_waiting"] == 2  # the owner reads everyone's time
    financial = data["financial"]
    # Approval is required: only the approved July entry (108 days old) and the disbursement.
    assert financial["wip_total"] == 12_000 + 1_300
    assert financial["wip_over_90_days"] == 12_000
    assert financial["debtors_total"] == 1_200 + 600
    assert financial["overdue_total"] == 1_200
    assert (financial["billed_this_month"], financial["billed_last_month"]) == (1_000, 500)
    assert [
        (c["client_name"], c["amount"], c["oldest_date"]) for c in financial["top_clients_by_wip"]
    ] == [
        ("Alpha Ltd", 12_000, "2026-07-01"),
        ("Beta LLP", 1_300, "2026-10-01"),
    ]
    assert [
        (c["client_id"], c["amount"], c["oldest_date"]) for c in financial["top_overdue_debtors"]
    ] == [(str(alpha.id), 1_200, "2026-10-16")]


async def test_dashboard_for_staff_has_no_practice_figures(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, org, member, *_ = await _dataset(client, monkeypatch)
    data = await _get(client, member, org)
    assert data["financial"] is None
    assert data["approvals_waiting"] is None
    assert data["timer"] is None
    assert data["jobs_due"] == []
    assert data["week"]["total_minutes"] == 0
    assert data["unsubmitted_week_count"] == 0  # the submitted week is not "unsubmitted"


async def test_approvals_waiting_counts_managed_people_without_read_all(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, org, member, *_ = await _dataset(client, monkeypatch)
    await custom_role(org, "Team lead", {"timesheet.approve", "time.log"})
    lead = await add_member(org["id"], "Team lead")
    await set_member(org, member, manager_user_id=lead.id)
    data = await _get(client, lead, org)
    assert data["approvals_waiting"] == 1
    assert data["financial"] is None
    manager = await staff(org, "manager")  # has time.read_all
    assert (await _get(client, manager, org))["approvals_waiting"] == 2


async def test_dashboard_tenant_isolation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, org, *_ = await _dataset(client, monkeypatch)
    other_owner, other = await practice(client, "Other & Co")
    response = await client.get(dashboard_url(org), headers=other_owner.headers)
    assert response.status_code == 404
    assert error(response)["code"] == "RESOURCE_NOT_FOUND"
    data = await _get(client, other_owner, other)
    assert data["financial"]["wip_total"] == 0
    assert data["financial"]["debtors_total"] == 0
    assert data["jobs_due"] == [] and data["timer"] is None


async def test_paused_timer_and_empty_practice(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin_now(monkeypatch, NOW)
    owner, org = await practice(client)
    await make_timer(org, owner, started_at=None, accumulated_seconds=125)
    data = await _get(client, owner, org)
    assert (data["timer"]["is_running"], data["timer"]["elapsed_seconds"]) == (False, 125)
    assert data["financial"] == {
        "wip_total": 0,
        "wip_over_90_days": 0,
        "debtors_total": 0,
        "overdue_total": 0,
        "billed_this_month": 0,
        "billed_last_month": 0,
        "top_clients_by_wip": [],
        "top_overdue_debtors": [],
    }
    assert data["week"]["utilisation_bp"] == 0


async def test_dashboard_uses_a_fixed_number_of_queries(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, org, *_ = await _dataset(client, monkeypatch)
    statements: list[str] = []

    def count(conn, cursor, statement, *args) -> None:
        statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        await _get(client, owner, org)
        first = len(statements)
        # More data must not mean more queries.
        alpha = await make_client(org, "More Ltd")
        for day in range(1, 8):
            await make_time_entry(org, owner, client=alpha, entry_date=date(2026, 9, day))
        statements.clear()
        await _get(client, owner, org)
        assert len(statements) == first
        assert first <= 20
    finally:
        event.remove(engine, "before_cursor_execute", count)


async def test_dashboards_in_parallel(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, org, member, *_ = await _dataset(client, monkeypatch)
    results = await asyncio.gather(*(_get(client, a, org) for a in (owner, member) * 4))
    assert {r["week"]["total_minutes"] for r in results[::2]} == {120}
    assert all(r["financial"] is None for r in results[1::2])

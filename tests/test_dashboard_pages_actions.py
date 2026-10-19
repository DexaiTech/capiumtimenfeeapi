"""My Actions: what the signed-in person has to do, and in which order."""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from app.db.models import Job, JobBudgetAlert, Task, TaskAssignee
from app.db.session import get_engine, get_sessionmaker
from tests.dashboard_page_helpers import (
    Cast,
    draft_by,
    get_page,
    pin_clock,
    seed_approvals,
    seed_money,
    seed_time,
    team,
)
from tests.domain_factories import make_client, make_job, make_time_entry, practice
from tests.expense_helpers import make_claim
from tests.factories import Actor, add_member
from tests.reports_helpers import LineSpec, assign, make_invoice, set_member


@dataclass
class World:
    c: Cast
    near: Job  # J00001, due 24 October, the owner manages it
    late: Job  # J00002, due 10 October, the owner is assigned


async def _task(
    org: dict, job: Job, title: str, due: date | None, who: Actor, status: str = "in_progress"
) -> None:
    org_id = uuid.UUID(org["id"])
    row = Task(
        organization_id=org_id,
        job_id=job.id,
        title=title,
        status=status,
        due_date=due,
        completed_at=datetime.now(UTC) if status == "completed" else None,
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.flush()
        session.add(TaskAssignee(organization_id=org_id, task_id=row.id, user_id=who.id))
        await session.commit()


async def _alert(org: dict, job: Job, kind: str, threshold: int, usage_bp: int) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            JobBudgetAlert(
                organization_id=uuid.UUID(org["id"]),
                job_id=job.id,
                kind=kind,
                threshold=threshold,
                usage_bp=usage_bp,
            )
        )
        await session.commit()


async def _world(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> World:
    pin_clock(monkeypatch)
    c = await team(client)
    alpha = await seed_time(c)
    money = await seed_money(c, alpha)
    await seed_approvals(c)

    near = await make_job(
        c.org,
        alpha,
        "Near",
        number="J00001",
        due_date=date(2026, 10, 24),
        manager_user_id=c.owner.id,
    )
    late = await make_job(c.org, money.beta, "Late", number="J00002", due_date=date(2026, 10, 10))
    await assign(c.org, late, c.owner)
    far = await make_job(
        c.org,
        alpha,
        "Far",
        number="J00003",
        due_date=date(2026, 10, 30),
        manager_user_id=c.owner.id,
    )
    done = await make_job(
        c.org,
        alpha,
        "Done",
        number="J00004",
        due_date=date(2026, 10, 18),
        manager_user_id=c.owner.id,
        status="completed",
    )
    await make_job(
        c.org,
        alpha,
        "Later",
        number="J00005",
        due_date=date(2026, 11, 20),
        manager_user_id=c.owner.id,
    )
    await _alert(c.org, near, "minutes", 80, 8_500)
    await _alert(c.org, late, "minutes", 80, 8_100)
    await _alert(c.org, late, "fee", 80, 8_300)
    await _alert(c.org, late, "fee", 100, 10_400)
    await _alert(c.org, done, "minutes", 100, 10_500)  # a completed job raises no action
    assert far.id

    await _task(c.org, near, "Draft the accounts", date(2026, 10, 9), c.owner)
    await _task(c.org, near, "Due today", date(2026, 10, 17), c.owner)
    await _task(c.org, near, "Finished", date(2026, 10, 1), c.owner, "completed")
    await _task(c.org, near, "Sam's task", date(2026, 10, 1), c.sam)

    one = await make_invoice(c.org, alpha, [LineSpec(1_000)], issue_date=None, status="draft")
    two = await make_invoice(c.org, alpha, [LineSpec(500)], issue_date=None, status="draft")
    await draft_by(one, c.owner)
    await draft_by(two, c.owner)
    await make_claim(c.org, c.owner, status="returned")
    return World(c, near, late)


def _kinds(data: dict) -> list[str]:
    return [action["kind"] for action in data["actions"]]


async def test_the_owners_actions_in_priority_order(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(client, monkeypatch)
    data = await get_page(client, w.c.owner, w.c.org, "my-actions")

    assert data["as_at"] == "2026-10-17"
    assert data["visible"] is True
    assert (data["total"], data["counts"]) == (11, {"high": 4, "medium": 5, "low": 2})
    assert [(a["kind"], a["priority"]) for a in data["actions"]] == [
        ("expense_returned", "high"),
        ("task_overdue", "high"),
        ("job_overdue", "high"),
        ("budget_exceeded", "high"),
        ("timesheet_to_submit", "medium"),
        ("timesheet_approvals", "medium"),
        ("expense_approvals", "medium"),
        ("budget_warning", "medium"),
        ("job_due_soon", "medium"),
        ("job_due_soon", "low"),
        ("draft_invoices", "low"),
    ]
    by_kind: dict[str, list[dict]] = {}
    for action in data["actions"]:
        by_kind.setdefault(action["kind"], []).append(action)

    returned = by_kind["expense_returned"][0]
    assert (returned["count"], returned["date"], returned["link"]) == (
        1,
        "2026-09-11",
        "/expenses/mine",
    )
    task = by_kind["task_overdue"][0]
    assert (task["subject"], task["context"]) == ("Draft the accounts", "J00001 Near (Alpha Ltd)")
    assert (task["date"], task["days_until_due"], task["link"]) == (
        "2026-10-09",
        -8,
        f"/jobs/{w.near.id}",
    )
    overdue = by_kind["job_overdue"][0]
    assert overdue["id"] == f"job_overdue:{w.late.id}"
    assert (overdue["subject"], overdue["context"]) == ("J00002 Late", "Beta LLP")
    assert (overdue["date"], overdue["days_until_due"]) == ("2026-10-10", -7)
    # One action per job, for the highest threshold crossed.
    exceeded = by_kind["budget_exceeded"][0]
    assert (exceeded["subject"], exceeded["usage_bp"]) == ("J00002 Late", 10_400)
    assert exceeded["link"] == f"/jobs/{w.late.id}"
    warning = by_kind["budget_warning"][0]
    assert (warning["subject"], warning["usage_bp"]) == ("J00001 Near", 8_500)

    submit = by_kind["timesheet_to_submit"][0]
    assert (submit["id"], submit["count"], submit["date"], submit["link"]) == (
        "timesheet_to_submit",
        1,
        "2026-10-05",
        "/time",
    )
    approvals = by_kind["timesheet_approvals"][0]
    assert (approvals["count"], approvals["date"], approvals["link"]) == (
        1,
        "2026-10-05",
        "/time/approvals",
    )
    claims = by_kind["expense_approvals"][0]
    assert (claims["count"], claims["date"], claims["link"]) == (
        1,
        "2026-09-10",
        "/expenses/approvals",
    )
    assert [
        (a["subject"], a["days_until_due"], a["priority"]) for a in by_kind["job_due_soon"]
    ] == [
        ("J00001 Near", 7, "medium"),
        ("J00003 Far", 13, "low"),
    ]
    drafts = by_kind["draft_invoices"][0]
    assert (drafts["count"], drafts["amount"], drafts["link"]) == (2, 1_800, "/billing/invoices")


async def test_the_limit_cuts_the_list_but_not_the_counts(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(client, monkeypatch)
    data = await get_page(client, w.c.owner, w.c.org, "my-actions", limit=3)
    assert _kinds(data) == ["expense_returned", "task_overdue", "job_overdue"]
    assert (data["total"], data["counts"]["high"]) == (11, 4)
    for bad in (0, 101):
        error = await get_page(client, w.c.owner, w.c.org, "my-actions", status=422, limit=bad)
        assert error["code"] == "VALIDATION_ERROR"


async def test_returned_and_missing_timesheets(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(client, monkeypatch)
    olly = w.c.olly
    # Olly's timesheet of 28 September was returned: it now holds an hour. Last week has nothing.
    await make_time_entry(
        w.c.org, olly, entry_date=date(2026, 9, 29), minutes=60, timesheet_status="rejected"
    )
    data = await get_page(client, olly, w.c.org, "my-actions")
    assert [
        (a["kind"], a["priority"], a["count"], a["date"], a["link"]) for a in data["actions"]
    ] == [
        ("timesheet_returned", "high", 1, "2026-09-28", "/time"),
        ("timesheet_missing", "medium", 1, "2026-10-05", "/time"),
    ]


async def test_a_week_with_time_is_not_missing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(client, monkeypatch)
    await make_time_entry(
        w.c.org, w.c.olly, entry_date=date(2026, 10, 7), minutes=60, timesheet_status="open"
    )
    data = await get_page(client, w.c.olly, w.c.org, "my-actions")
    # The open sheet of last week holds time: it has to be submitted, it is not missing.
    assert [(a["kind"], a["count"]) for a in data["actions"]] == [("timesheet_to_submit", 1)]


async def test_staff_with_nothing_to_do(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(client, monkeypatch)
    zed = await add_member(w.c.org["id"], "staff", full_name="Zed New")
    await set_member(w.c.org, zed, start_date=date(2026, 10, 12))  # not expected last week
    data = await get_page(client, zed, w.c.org, "my-actions")
    assert (data["total"], data["counts"], data["actions"]) == (
        0,
        {"high": 0, "medium": 0, "low": 0},
        [],
    )


async def test_a_person_gets_only_their_own_tasks(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(client, monkeypatch)
    data = await get_page(client, w.c.sam, w.c.org, "my-actions")
    assert [(a["kind"], a["subject"], a["days_until_due"]) for a in data["actions"]] == [
        ("task_overdue", "Sam's task", -16)
    ]


async def test_a_manager_has_the_approvals_waiting(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(client, monkeypatch)
    data = await get_page(client, w.c.mia, w.c.org, "my-actions")
    assert [(a["kind"], a["priority"], a["count"]) for a in data["actions"]] == [
        ("timesheet_approvals", "medium", 1),
        ("expense_approvals", "medium", 1),
    ]


async def test_drafts_need_the_permission_to_prepare_invoices(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(client, monkeypatch)
    alpha = await make_client(w.c.org, "Delta Ltd")
    billing = await add_member(w.c.org["id"], "billing", full_name="Bea Billing")
    mine = await make_invoice(w.c.org, alpha, [LineSpec(300)], issue_date=None, status="draft")
    await draft_by(mine, billing)
    others = await make_invoice(w.c.org, alpha, [LineSpec(300)], issue_date=None, status="draft")
    await draft_by(others, w.c.sam)  # Staff cannot prepare invoices: nothing to finish
    data = await get_page(client, billing, w.c.org, "my-actions")
    assert [(a["kind"], a["count"], a["amount"]) for a in data["actions"]] == [
        ("draft_invoices", 1, 360)
    ]
    assert (await get_page(client, w.c.sam, w.c.org, "my-actions"))["total"] == 1  # his task


async def test_a_member_who_does_not_record_time_has_no_timesheet_actions(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(client, monkeypatch)
    viewer = await add_member(w.c.org["id"], "viewer")
    assert (await get_page(client, viewer, w.c.org, "my-actions"))["actions"] == []


async def test_tenant_isolation(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    w = await _world(client, monkeypatch)
    other, other_org = await practice(client, "Other & Co")
    refused = await get_page(client, other, w.c.org, "my-actions", status=404)
    assert refused["code"] == "RESOURCE_NOT_FOUND"
    data = await get_page(client, other, other_org, "my-actions")
    # Nothing of the first practice shows: only the new owner's own empty last week.
    assert _kinds(data) == ["timesheet_missing"]


async def test_the_number_of_queries_does_not_depend_on_the_data(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = await _world(client, monkeypatch)
    statements: list[str] = []

    def count(conn, cursor, statement, *args) -> None:
        statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        await get_page(client, w.c.owner, w.c.org, "my-actions")
        first = len(statements)
        gamma = await make_client(w.c.org, "Gamma Ltd")
        for number in range(6, 12):
            job = await make_job(
                w.c.org,
                gamma,
                f"More {number}",
                number=f"J{number:05d}",
                due_date=date(2026, 10, 12),
                manager_user_id=w.c.owner.id,
            )
            await _task(w.c.org, job, f"Task {number}", date(2026, 10, 1), w.c.owner)
            await _alert(w.c.org, job, "minutes", 80, 8_200)
        statements.clear()
        data = await get_page(client, w.c.owner, w.c.org, "my-actions")
        assert data["total"] == 11 + 18
        assert len(statements) == first
        assert first <= 45
    finally:
        event.remove(engine, "before_cursor_execute", count)

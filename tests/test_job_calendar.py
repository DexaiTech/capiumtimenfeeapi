"""The job calendar: jobs overlapping a range and tasks due in it."""

import pytest
from httpx import AsyncClient

from tests.domain_factories import make_client, practice, staff
from tests.job_helpers import add_task, create_job, job_url, log_on_job, role_without, set_job_row
from tests.time_helpers import err, ok_data, org_url


async def _calendar(client: AsyncClient, actor, org, start: str, end: str, **params):
    response = await client.get(
        org_url(org, "/jobs/calendar"),
        headers=actor.headers,
        params={"from": start, "to": end, **params},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _titles(calendar: dict) -> list[tuple[str, str]]:
    return [(event["type"], event["title"]) for event in calendar["events"]]


async def test_jobs_that_overlap_the_range_and_tasks_due_in_it(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme, beta = await make_client(org, "Acme Ltd"), await make_client(org, "Beta plc")
    both = await create_job(
        client, owner, org, acme, "Accounts", start_date="2026-03-01", due_date="2026-03-31"
    )
    await create_job(client, owner, org, acme, "VAT return", due_date="2026-03-15")
    payroll = await create_job(client, owner, org, beta, "Payroll", start_date="2026-02-10")
    before = await create_job(
        client, owner, org, beta, "Old", start_date="2026-01-01", due_date="2026-01-31"
    )
    await create_job(
        client, owner, org, beta, "Later", start_date="2026-05-01", due_date="2026-05-31"
    )
    await create_job(client, owner, org, beta, "No dates")
    await add_task(client, owner, org, both, "Bookkeeping", due_date="2026-03-10")
    # Payroll has no due date, so a task due in April is valid there but falls outside March.
    await add_task(client, owner, org, payroll, "Out of range", due_date="2026-04-10")
    await add_task(client, owner, org, before, "Early task", due_date="2026-01-15")

    calendar = await _calendar(client, owner, org, "2026-03-01", "2026-03-31")
    assert (calendar["from"], calendar["to"]) == ("2026-03-01", "2026-03-31")
    assert _titles(calendar) == [
        ("job", "Payroll"),  # started on 10 Feb and runs on
        ("job", "Accounts"),
        ("task", "Bookkeeping"),
        ("job", "VAT return"),
    ]
    events = {e["title"]: e for e in calendar["events"]}
    accounts = events["Accounts"]
    assert (accounts["start"], accounts["end"], accounts["due"]) == (
        "2026-03-01",
        "2026-03-31",
        "2026-03-31",
    )
    assert accounts["id"] == both["id"] and accounts["job_id"] == both["id"]
    assert accounts["job_number"] == both["number"] and accounts["status"] == "planned"
    assert accounts["client_id"] == str(acme.id) and accounts["client_name"] == "Acme Ltd"
    assert accounts["assignees"] == [] and accounts["budget_used_bp"] is None
    vat = events["VAT return"]
    assert (vat["start"], vat["end"], vat["due"]) == ("2026-03-15", "2026-03-15", "2026-03-15")
    payroll = events["Payroll"]
    assert (payroll["start"], payroll["end"], payroll["due"]) == ("2026-02-10", None, None)
    task = events["Bookkeeping"]
    assert task["type"] == "task" and task["id"] != both["id"] and task["job_id"] == both["id"]
    assert (task["start"], task["end"], task["due"]) == ("2026-03-10", "2026-03-10", "2026-03-10")
    assert task["status"] == "not_started" and task["job_number"] == both["number"]
    assert task["client_name"] == "Acme Ltd"

    # A one-day range and the edges of a job's span are inclusive.
    assert _titles(await _calendar(client, owner, org, "2026-03-31", "2026-03-31")) == [
        ("job", "Payroll"),
        ("job", "Accounts"),
    ]
    assert _titles(await _calendar(client, owner, org, "2026-02-28", "2026-02-28")) == [
        ("job", "Payroll")
    ]
    assert _titles(await _calendar(client, owner, org, "2026-01-01", "2026-01-31")) == [
        ("job", "Old"),
        ("task", "Early task"),
    ]
    assert _titles(await _calendar(client, owner, org, "2025-01-01", "2025-12-31")) == []


async def test_a_finished_job_with_only_a_start_date_is_its_start_day(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org)
    job = await create_job(client, owner, org, acme, "Payroll", start_date="2026-02-10")
    assert _titles(await _calendar(client, owner, org, "2026-03-01", "2026-03-31")) == [
        ("job", "Payroll")
    ]
    for status in ("completed", "closed", "cancelled"):
        await set_job_row(job["id"], status=status)
        assert _titles(await _calendar(client, owner, org, "2026-03-01", "2026-03-31")) == []
        events = (await _calendar(client, owner, org, "2026-02-01", "2026-02-28"))["events"]
        assert [(e["title"], e["start"], e["end"], e["status"]) for e in events] == [
            ("Payroll", "2026-02-10", "2026-02-10", status)
        ]


async def test_filters(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme, beta = await make_client(org, "Acme Ltd"), await make_client(org, "Beta plc")
    ann, bob = (
        await staff(org, full_name="Ann Able"),
        await staff(org, "manager", full_name="Bob Baker"),
    )
    j1 = await create_job(
        client, owner, org, acme, "Accounts", start_date="2026-03-01", due_date="2026-03-31",
        manager_user_id=bob.id, assignee_user_ids=[ann.id],
    )  # fmt: skip
    j2 = await create_job(
        client, owner, org, beta, "Payroll", start_date="2026-03-05", due_date="2026-03-20",
        status="in_progress",
    )  # fmt: skip
    await add_task(
        client, owner, org, j1, "Draft", due_date="2026-03-10", assignee_user_ids=[ann.id]
    )
    await add_task(client, owner, org, j1, "Review", due_date="2026-03-11", status="in_progress")
    await add_task(
        client, owner, org, j2, "Payslips", due_date="2026-03-12", assignee_user_ids=[bob.id]
    )
    rng = ("2026-03-01", "2026-03-31")

    async def titles(**params) -> list[str]:
        return [t for _, t in _titles(await _calendar(client, owner, org, *rng, **params))]

    assert await titles(client_id=acme.id) == ["Accounts", "Draft", "Review"]
    assert await titles(client_id=beta.id) == ["Payroll", "Payslips"]
    # The manager of a job counts for its tasks too.
    assert await titles(manager_user_id=bob.id) == ["Accounts", "Draft", "Review"]
    # An assignee counts for the job and the tasks assigned to them.
    assert await titles(assignee_user_id=ann.id) == ["Accounts", "Draft"]
    assert await titles(assignee_user_id=bob.id) == ["Payslips"]
    assert await titles(assignee_user_id="me") == []
    assert await titles(status="in_progress") == ["Payroll", "Review"]
    # `planned` and `not_started` are the same word for jobs and tasks.
    assert await titles(status="planned") == ["Accounts", "Draft", "Payslips"]
    assert await titles(status="not_started") == ["Accounts", "Draft", "Payslips"]
    assert await titles(status=["planned", "in_progress"]) == [
        "Accounts", "Payroll", "Draft", "Review", "Payslips",
    ]  # fmt: skip
    assert await titles(status="completed") == []
    assert await titles(client_id=acme.id, assignee_user_id=ann.id, status="in_progress") == []
    events = (await _calendar(client, owner, org, *rng, assignee_user_id=ann.id))["events"]
    assert [a["full_name"] for a in events[0]["assignees"]] == ["Ann Able"]


async def test_the_budget_usage_of_events(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org)
    job = await create_job(
        client, owner, org, acme, "Accounts", start_date="2026-03-01", due_date="2026-03-31",
        budget_minutes=600, hourly_rate=6000,
    )  # fmt: skip
    task = await add_task(
        client, owner, org, job, "Draft", due_date="2026-03-10", budget_minutes=120
    )
    await log_on_job(client, owner, org, job, 300, task_id=task["id"])
    events = {
        e["title"]: e
        for e in (await _calendar(client, owner, org, "2026-03-01", "2026-03-31"))["events"]
    }
    assert events["Accounts"]["budget_used_bp"] == 5000
    assert events["Draft"]["budget_used_bp"] == 25000


async def test_range_rules(client: AsyncClient) -> None:
    owner, org = await practice(client)
    url = org_url(org, "/jobs/calendar")

    async def ask(**params):
        return await client.get(url, headers=owner.headers, params=params)

    ok = await ask(**{"from": "2026-01-01", "to": "2027-01-02"})  # 366 days apart
    assert ok.status_code == 200
    long = err(await ask(**{"from": "2026-01-01", "to": "2027-01-03"}), 422)
    assert long["details"][0]["field"] == "to" and long["details"][0]["code"] == "range_too_long"
    backwards = err(await ask(**{"from": "2026-03-02", "to": "2026-03-01"}), 422)
    assert backwards["details"][0]["field"] == "to"
    for params in (
        {},
        {"from": "2026-03-01"},
        {"to": "2026-03-01"},
        {"from": "nope", "to": "2026-03-01"},
        {"from": "2026-03-01", "to": "2026-03-31", "assignee_user_id": "nobody"},
        {"from": "2026-03-01", "to": "2026-03-31", "manager_user_id": "nobody"},
        {"from": "2026-03-01", "to": "2026-03-31", "status": "bad"},
        {"from": "2026-03-01", "to": "2026-03-31", "client_id": "not-a-uuid"},
    ):
        assert (await ask(**params)).status_code == 422, params


async def test_too_many_events(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, org = await practice(client)
    acme = await make_client(org)
    monkeypatch.setattr("app.services.job_calendar.MAX_EVENTS", 2)
    for n in range(3):
        await create_job(client, owner, org, acme, f"Job {n}", due_date="2026-03-15")
    error = err(
        await client.get(
            org_url(org, "/jobs/calendar"),
            headers=owner.headers,
            params={"from": "2026-03-01", "to": "2026-03-31"},
        ),
        422,
    )
    assert error["details"][0]["code"] == "too_many_events"
    # A narrower view of the same practice is fine.
    assert (await _calendar(client, owner, org, "2026-03-16", "2026-03-31"))["events"] == []


async def test_deleted_jobs_and_other_practices_are_not_shown(client: AsyncClient) -> None:
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other & Co")
    theirs = await create_job(
        client, other_owner, other, await make_client(other), "Theirs", due_date="2026-03-15"
    )
    await add_task(client, other_owner, other, theirs, "Secret", due_date="2026-03-15")
    gone = await create_job(
        client, owner, org, await make_client(org), "Gone", due_date="2026-03-15"
    )
    await add_task(client, owner, org, gone, "Gone task", due_date="2026-03-15")
    await create_job(client, owner, org, await make_client(org), "Kept", due_date="2026-03-15")
    assert (await client.delete(job_url(org, gone["id"]), headers=owner.headers)).status_code == 200
    assert _titles(await _calendar(client, owner, org, "2026-03-01", "2026-03-31")) == [
        ("job", "Kept")
    ]
    # The other practice's owner cannot read our calendar.
    response = await client.get(
        org_url(org, "/jobs/calendar"),
        headers=other_owner.headers,
        params={"from": "2026-03-01", "to": "2026-03-31"},
    )
    assert response.status_code == 404


async def test_access(client: AsyncClient) -> None:
    owner, org = await practice(client)
    params = {"from": "2026-03-01", "to": "2026-03-31"}
    url = org_url(org, "/jobs/calendar")
    for role in ("staff", "viewer", "billing", "manager"):
        person = await staff(org, role)
        assert (await client.get(url, headers=person.headers, params=params)).status_code == 200, (
            role
        )
    nobody = await role_without(org, "job.read")
    assert (await client.get(url, headers=nobody.headers, params=params)).status_code == 403
    assert (await client.get(url, params=params)).status_code == 401
    assert ok_data(await client.get(url, headers=owner.headers, params=params))["events"] == []

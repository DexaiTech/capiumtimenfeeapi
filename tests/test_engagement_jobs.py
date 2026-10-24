"""Jobs from client engagements: the period rules, the daily worker and the manual route."""

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.models import Permission, Role, RolePermission
from app.db.session import get_sessionmaker
from app.services.engagement_jobs import (
    Period,
    due_period,
    job_name,
    next_period,
    period_at,
    period_label,
    upcoming_period,
)
from app.services.engagement_jobs import run_due as run_engagement_jobs
from app.workers.handlers import HANDLERS, PERIODIC
from tests.clients_helpers import sql
from tests.domain_factories import make_client, practice, staff
from tests.factories import audit_entries, error
from tests.job_helpers import task_url
from tests.time_helpers import ok_data, org_url


def london_today() -> date:
    return datetime.now(ZoneInfo("Europe/London")).date()


def d(n: int) -> str:
    """The date n days from today (negative: ago) as the API writes it."""
    return (london_today() + timedelta(days=n)).isoformat()


# --- Period math (pure) -----------------------------------------------------------------------


def test_periods_are_counted_from_the_start_and_clamp_to_the_month_end() -> None:
    jan31 = date(2026, 1, 31)
    monthly = [period_at(jan31, "monthly", k) for k in range(4)]
    assert [p.start for p in monthly] == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
    ]
    # A period ends the day before the next one starts.
    assert [p.end for p in monthly] == [
        date(2026, 2, 27),
        date(2026, 3, 30),
        date(2026, 4, 29),
        date(2026, 5, 30),
    ]
    assert period_at(date(2026, 10, 1), "quarterly", 1) == Period(
        date(2027, 1, 1), date(2027, 3, 31)
    )
    assert period_at(date(2026, 10, 1), "annually", 0) == Period(
        date(2026, 10, 1), date(2027, 9, 30)
    )
    # A leap day: 29 February clamps in common years and comes back in leap years.
    leap = date(2024, 2, 29)
    assert [period_at(leap, "annually", k).start for k in (1, 2, 3, 4)] == [
        date(2025, 2, 28),
        date(2026, 2, 28),
        date(2027, 2, 28),
        date(2028, 2, 29),
    ]
    assert period_at(leap, "annually", 0).end == date(2025, 2, 27)


def test_a_one_off_engagement_has_one_period_without_an_end() -> None:
    assert period_at(date(2026, 5, 5), "one_off", 0) == Period(date(2026, 5, 5), None)
    with pytest.raises(ValueError):
        period_at(date(2026, 5, 5), "one_off", 1)


def test_labels_and_names() -> None:
    month = period_at(date(2026, 10, 1), "monthly", 0)
    quarter = period_at(date(2026, 10, 1), "quarterly", 0)
    year = period_at(date(2026, 10, 1), "annually", 0)
    assert period_label("monthly", month) == "Oct 2026"
    assert period_label("quarterly", quarter) == "Oct 2026 - Dec 2026"
    assert period_label("annually", year) == "Year to Sep 2027"
    assert period_label("one_off", Period(date(2026, 10, 1), None)) is None
    assert job_name("Bookkeeping", "monthly", month) == "Bookkeeping - Oct 2026"
    assert job_name("Accounts", "annually", year) == "Accounts - Year to Sep 2027"
    assert job_name("Tax return", "one_off", Period(date(2026, 10, 1), None)) == "Tax return"
    assert len(job_name("x" * 300, "monthly", month)) == 200


@pytest.mark.parametrize(
    ("today", "lead", "expected"),
    [
        # The window has not opened: nothing is due.
        (date(2026, 9, 30), 0, None),
        (date(2026, 10, 1), 0, date(2026, 10, 1)),
        # Lead days open the window early.
        (date(2026, 9, 23), 7, None),
        (date(2026, 9, 24), 7, date(2026, 10, 1)),
        # Never backfill: months later only the latest period is due.
        (date(2026, 12, 15), 0, date(2026, 12, 1)),
        # The next period opens before the current one ends: it replaces it.
        (date(2026, 10, 27), 5, date(2026, 11, 1)),
        (date(2026, 10, 26), 5, date(2026, 10, 1)),
    ],
)
def test_the_due_period_is_the_latest_one_whose_window_has_opened(
    today: date, lead: int, expected: date | None
) -> None:
    found = due_period(date(2026, 10, 1), "monthly", lead, None, today)
    assert (found.start if found else None) == expected


def test_a_period_after_the_end_date_is_never_due() -> None:
    anchor, end = date(2026, 10, 1), date(2026, 12, 15)
    assert due_period(anchor, "monthly", 0, end, date(2026, 12, 1)) == period_at(
        anchor, "monthly", 2
    )
    # January starts after the end date: no job (and no fallback to an older period).
    assert due_period(anchor, "monthly", 0, end, date(2027, 1, 5)) is None
    assert upcoming_period(anchor, "monthly", 0, end, date(2026, 12, 20)) is None


def test_one_off_is_due_once_its_window_opens() -> None:
    start = date(2026, 10, 10)
    assert due_period(start, "one_off", 0, None, date(2026, 10, 9)) is None
    assert due_period(start, "one_off", 3, None, date(2026, 10, 7)) == Period(start, None)
    assert due_period(start, "one_off", 0, None, date(2027, 6, 1)) == Period(start, None)


def test_the_next_period_skips_one_that_has_its_job() -> None:
    anchor = date(2026, 10, 1)
    today = date(2026, 10, 24)
    assert next_period(anchor, "monthly", 0, None, today, set()) == period_at(anchor, "monthly", 0)
    assert next_period(anchor, "monthly", 0, None, today, {anchor}) == period_at(
        anchor, "monthly", 1
    )
    # Before the start the first period is the upcoming one.
    assert next_period(anchor, "monthly", 0, None, date(2026, 9, 1), set()) == period_at(
        anchor, "monthly", 0
    )
    one_off = date(2026, 10, 1)
    assert next_period(one_off, "one_off", 0, None, today, {one_off}) is None
    assert next_period(one_off, "one_off", 0, None, date(2026, 9, 1), set()) == Period(
        one_off, None
    )


# --- Setup ------------------------------------------------------------------------------------


async def make_service(client: AsyncClient, owner, org, code: str = "ZZ1", **body) -> dict:
    response = await client.post(
        org_url(org, "/services"),
        headers=owner.headers,
        json={"code": code, "name": f"Service {code}"} | body,
    )
    return ok_data(response, 201)


async def add_templates(client: AsyncClient, owner, org, service: dict) -> None:
    response = await client.put(
        org_url(org, f"/services/{service['id']}/task-templates"),
        headers=owner.headers,
        json={"templates": [{"title": "Prepare", "due_offset_days": 3}, {"title": "Review"}]},
    )
    assert response.status_code == 200, response.text


async def engage(client: AsyncClient, owner, org, customer, service: dict, **body) -> dict:
    response = await client.post(
        org_url(org, f"/clients/{customer.id}/services"),
        headers=owner.headers,
        json={"service_id": service["id"], **body},
    )
    return ok_data(response, 201)


async def run_worker() -> dict:
    async with get_sessionmaker()() as session:
        return await run_engagement_jobs(session)


async def runs_of(client: AsyncClient, owner, org, customer, engagement: dict) -> list[dict]:
    return ok_data(
        await client.get(
            org_url(org, f"/clients/{customer.id}/services/{engagement['id']}/job-runs"),
            headers=owner.headers,
        )
    )


async def jobs_of(client: AsyncClient, owner, org, customer) -> list[dict]:
    return ok_data(
        await client.get(
            org_url(org, "/jobs"), headers=owner.headers, params={"client_id": str(customer.id)}
        )
    )


def create_job_url(org: dict, customer, engagement: dict) -> str:
    return org_url(org, f"/clients/{customer.id}/services/{engagement['id']}/create-job")


async def set_run_age(engagement_id: str, days: int) -> None:
    """Make the runs of an engagement look ``days`` old (the trigger keeps ``updated_at`` current,
    so it is switched off for the statement)."""
    await sql(
        "ALTER TABLE engagement_job_runs DISABLE TRIGGER trg_engagement_job_runs_set_updated_at"
    )
    try:
        await sql(
            "UPDATE engagement_job_runs SET updated_at = now() - make_interval(days => :n) "
            "WHERE client_service_id = :e",
            n=days,
            e=engagement_id,
        )
    finally:
        await sql(
            "ALTER TABLE engagement_job_runs ENABLE TRIGGER trg_engagement_job_runs_set_updated_at"
        )


# --- The worker -------------------------------------------------------------------------------


async def test_the_worker_is_registered_daily() -> None:
    assert ("create_engagement_jobs", 86_400) in PERIODIC
    assert "create_engagement_jobs" in HANDLERS


async def test_a_due_engagement_gets_its_job_with_tasks_dates_and_people(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    manager = await staff(org, "manager")
    customer = await make_client(org, manager_user_id=manager.id, partner_user_id=owner.id)
    service = await make_service(
        client, owner, org, name="Monthly bookkeeping", billing_basis="fixed", default_fee=50_000
    )
    await add_templates(client, owner, org, service)
    start = london_today().replace(day=1)
    engagement = await engage(
        client, owner, org, customer, service, frequency="monthly", start_date=start.isoformat(),
        fee=75_000, auto_create_jobs=True,
    )  # fmt: skip
    assert engagement["auto_create_jobs"] is True
    assert engagement["next_job_period"]["period_start"] == start.isoformat()

    assert (await run_worker())["created"] == 1

    [job] = await jobs_of(client, owner, org, customer)
    month = start.strftime("%b %Y")
    assert job["name"] == f"Monthly bookkeeping - {month}"
    assert job["status"] == "planned"
    assert (job["period_start"], job["start_date"], job["due_date"]) == (
        start.isoformat(),
        start.isoformat(),
        None,
    )
    assert job["period_end"] == period_at(start, "monthly", 0).end.isoformat()
    assert job["manager_user_id"] == str(manager.id)
    assert job["partner_user_id"] == str(owner.id)
    assert (job["billing_basis"], job["fixed_fee"]) == ("fixed", 75_000)
    assert job["assignee_user_ids"] == []
    tasks = ok_data(await client.get(task_url(org, job["id"]), headers=owner.headers))
    assert sorted(t["title"] for t in tasks) == ["Prepare", "Review"]

    [run] = await runs_of(client, owner, org, customer, engagement)
    assert run["status"] == "created" and run["job"]["id"] == job["id"]
    assert run["job"]["number"] == job["number"]
    after = ok_data(
        await client.get(org_url(org, f"/clients/{customer.id}"), headers=owner.headers)
    )["engagements"][0]
    assert after["next_job_period"]["period_start"] != start.isoformat()


async def test_running_twice_creates_one_job_and_a_deleted_job_is_not_recreated(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    service = await make_service(client, owner, org)
    engagement = await engage(
        client, owner, org, customer, service, frequency="monthly", start_date=d(-3),
        auto_create_jobs=True,
    )  # fmt: skip
    first = await run_worker()
    second = await run_worker()
    assert (first["created"], second["created"]) == (1, 0)
    [job] = await jobs_of(client, owner, org, customer)

    # Deleting the job (soft) leaves the run, so the next run does not make it again.
    deleted = await client.delete(org_url(org, f"/jobs/{job['id']}"), headers=owner.headers)
    assert deleted.status_code == 200, deleted.text
    assert (await run_worker())["created"] == 0
    assert await jobs_of(client, owner, org, customer) == []
    [run] = await runs_of(client, owner, org, customer, engagement)
    assert run["status"] == "created" and run["job"] is None

    # A hard delete sets the link to null; the period still has its row.
    await sql("UPDATE engagement_job_runs SET job_id = NULL WHERE client_service_id = :e",
              e=engagement["id"])  # fmt: skip
    await sql("DELETE FROM jobs WHERE id = :j", j=job["id"])
    assert (await run_worker())["created"] == 0
    assert len(await runs_of(client, owner, org, customer, engagement)) == 1


async def test_the_worker_skips_what_is_not_due(client: AsyncClient) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    service = await make_service(client, owner, org)
    other = await make_service(client, owner, org, "ZZ2")
    third = await make_service(client, owner, org, "ZZ3")
    fourth = await make_service(client, owner, org, "ZZ4")
    # Off, not started yet (7 days of lead is not enough), inactive, ended before the start.
    await engage(client, owner, org, customer, service, frequency="monthly", start_date=d(-3))
    await engage(
        client, owner, org, customer, other, frequency="monthly", start_date=d(20),
        auto_create_jobs=True, job_lead_days=7,
    )  # fmt: skip
    await engage(
        client, owner, org, customer, third, frequency="monthly", start_date=d(-3),
        auto_create_jobs=True, is_active=False,
    )  # fmt: skip
    await engage(
        client, owner, org, customer, fourth, frequency="monthly", start_date=d(-10),
        end_date=d(-5), auto_create_jobs=True,
    )  # fmt: skip
    # The window of the last one is open and its first period starts before the end date.
    assert (await run_worker())["created"] == 1
    [job] = await jobs_of(client, owner, org, customer)
    assert job["service_name"] == "Service ZZ4"


async def test_the_lead_days_open_the_window_early_and_never_backfill(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    early = await make_service(client, owner, org, "ZE1")
    late = await make_service(client, owner, org, "ZL1")
    await engage(
        client, owner, org, customer, early, frequency="annually", start_date=d(10),
        auto_create_jobs=True, job_lead_days=14,
    )  # fmt: skip
    # Started two years ago: one job for the current year, none for the years before.
    two_years = london_today().replace(year=london_today().year - 2, day=1)
    await engage(
        client, owner, org, customer, late, frequency="annually",
        start_date=two_years.isoformat(), auto_create_jobs=True,
    )  # fmt: skip
    assert (await run_worker())["created"] == 2
    jobs = await jobs_of(client, owner, org, customer)
    assert len(jobs) == 2
    year_job = next(j for j in jobs if j["service_name"] == "Service ZL1")
    assert year_job["period_start"] > two_years.replace(year=two_years.year + 1).isoformat()
    assert year_job["name"].startswith("Service ZL1 - Year to ")


async def test_a_one_off_engagement_gets_one_job_named_after_the_service(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    service = await make_service(client, owner, org, name="Company formation")
    engagement = await engage(
        client, owner, org, customer, service, frequency="one_off", start_date=d(-1),
        auto_create_jobs=True,
    )  # fmt: skip
    await run_worker()
    await run_worker()
    [job] = await jobs_of(client, owner, org, customer)
    assert job["name"] == "Company formation"
    assert job["period_end"] is None
    got = ok_data(await client.get(org_url(org, f"/clients/{customer.id}/services"),
                                   headers=owner.headers))  # fmt: skip
    assert got[0]["next_job_period"] is None
    assert engagement["frequency"] == "one_off"


async def test_a_failed_client_is_recorded_and_retried_only_on_a_later_day(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    service = await make_service(client, owner, org)
    engagement = await engage(
        client, owner, org, customer, service, frequency="monthly", start_date=d(-2),
        auto_create_jobs=True,
    )  # fmt: skip
    await sql("UPDATE clients SET status = 'archived' WHERE id = :c", c=str(customer.id))
    assert (await run_worker())["failed"] == 1
    [run] = await runs_of(client, owner, org, customer, engagement)
    assert run["status"] == "failed" and run["job"] is None
    assert "client" in run["error"].lower()

    # The same day: not tried again (the failure is not rewritten either).
    again = await run_worker()
    assert (again["created"], again["failed"]) == (0, 0)

    # A later day, the client accepts work again: the period is created.
    await sql("UPDATE clients SET status = 'active' WHERE id = :c", c=str(customer.id))
    assert (await run_worker())["created"] == 0
    await set_run_age(engagement["id"], 1)
    assert (await run_worker())["created"] == 1
    [run] = await runs_of(client, owner, org, customer, engagement)
    assert run["status"] == "created" and run["error"] is None and run["job"] is not None
    assert len(await jobs_of(client, owner, org, customer)) == 1

    # Failing again on a later day keeps one row and refreshes its reason.
    second = await engage(
        client, owner, org, customer, await make_service(client, owner, org, "ZZ2"),
        frequency="monthly", start_date=d(-2), auto_create_jobs=True,
    )  # fmt: skip
    await sql("UPDATE clients SET status = 'inactive' WHERE id = :c", c=str(customer.id))
    await run_worker()
    await set_run_age(second["id"], 2)
    assert (await run_worker())["failed"] == 1
    assert len(await runs_of(client, owner, org, customer, second)) == 1


async def test_the_scheduled_job_is_audited_as_the_schedule(client: AsyncClient) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    service = await make_service(client, owner, org)
    engagement = await engage(
        client, owner, org, customer, service, frequency="quarterly", start_date=d(-1),
        auto_create_jobs=True,
    )  # fmt: skip
    await run_worker()
    [entry] = await audit_entries(org["id"], "engagement.job_created")
    assert entry["target_type"] == "client_service" and entry["target_id"] == engagement["id"]
    assert entry["actor_user_id"] == str(owner.id)
    meta = entry["metadata"]
    assert meta["source"] == "engagement_schedule"
    assert meta["period_start"] == d(-1) and meta["job_number"]
    [job_entry] = await audit_entries(org["id"], "job.created")
    assert job_entry["metadata"]["source"] == "engagement_schedule"


async def test_a_practice_is_isolated_from_the_others(client: AsyncClient) -> None:
    owner, org = await practice(client)
    other_owner, other_org = await practice(client, "Other Accountants")
    customer = await make_client(org)
    other_customer = await make_client(other_org)
    for o, who, c in ((org, owner, customer), (other_org, other_owner, other_customer)):
        service = await make_service(client, who, o)
        await engage(
            client, who, o, c, service, frequency="monthly", start_date=d(-1),
            auto_create_jobs=True,
        )  # fmt: skip
    assert (await run_worker())["created"] == 2
    assert len(await jobs_of(client, owner, org, customer)) == 1
    assert len(await jobs_of(client, other_owner, other_org, other_customer)) == 1
    assert [
        e["actor_user_id"] for e in await audit_entries(other_org["id"], "engagement.job_created")
    ] == [str(other_owner.id)]


# --- Fields and validation --------------------------------------------------------------------


async def test_the_engagement_fields_default_and_validate(client: AsyncClient) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    service = await make_service(client, owner, org)
    other = await make_service(client, owner, org, "ZZ2")
    url = org_url(org, f"/clients/{customer.id}/services")

    plain = await engage(client, owner, org, customer, service)
    assert (plain["auto_create_jobs"], plain["job_lead_days"], plain["next_job_period"]) == (
        False,
        0,
        None,
    )

    no_start = await client.post(
        url, headers=owner.headers, json={"service_id": other["id"], "auto_create_jobs": True}
    )
    assert no_start.status_code == 422, no_start.text
    assert "start_date" in no_start.text
    for lead in (-1, 366):
        bad = await client.post(
            url,
            headers=owner.headers,
            json={"service_id": other["id"], "start_date": d(1), "job_lead_days": lead},
        )
        assert bad.status_code == 422, bad.text

    patch = org_url(org, f"/clients/{customer.id}/services/{plain['id']}")
    refused = await client.patch(patch, headers=owner.headers, json={"auto_create_jobs": True})
    assert refused.status_code == 422 and "start_date" in refused.text
    nulled = await client.patch(patch, headers=owner.headers, json={"job_lead_days": None})
    assert nulled.status_code == 422, nulled.text
    updated = ok_data(
        await client.patch(
            patch,
            headers=owner.headers,
            json={"auto_create_jobs": True, "start_date": d(30), "job_lead_days": 10},
        )
    )
    assert updated["auto_create_jobs"] is True and updated["job_lead_days"] == 10
    assert updated["next_job_period"]["create_on"] == d(20)
    # Clearing the start date of an opted-in engagement is refused too.
    cleared = await client.patch(patch, headers=owner.headers, json={"start_date": None})
    assert cleared.status_code == 422, cleared.text
    off = ok_data(
        await client.patch(patch, headers=owner.headers, json={"auto_create_jobs": False})
    )
    assert off["next_job_period"] is None


# --- The manual route -------------------------------------------------------------------------


async def test_create_job_now_then_again_is_a_conflict(client: AsyncClient) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    service = await make_service(client, owner, org, name="Payroll run")
    await add_templates(client, owner, org, service)
    engagement = await engage(
        client, owner, org, customer, service, frequency="monthly", start_date=d(-40)
    )
    response = await client.post(create_job_url(org, customer, engagement), headers=owner.headers)
    created = ok_data(response, 201)
    job = created["job"]
    assert created["period_end"] >= d(0) and created["period_start"] <= d(0)
    assert job["name"].startswith("Payroll run - ") and job["status"] == "planned"
    assert job["task_counts"]["total"] == 2
    [entry] = await audit_entries(org["id"], "engagement.job_created")
    assert entry["metadata"]["source"] == "engagement_manual"

    again = await client.post(create_job_url(org, customer, engagement), headers=owner.headers)
    assert again.status_code == 409 and error(again)["code"] == "ENGAGEMENT_JOB_EXISTS"
    [run] = await runs_of(client, owner, org, customer, engagement)
    assert run["job"]["id"] == job["id"]
    # The worker agrees with the manual run: the period is done.
    assert (await run_worker())["created"] == 0
    assert len(await jobs_of(client, owner, org, customer)) == 1


async def test_create_job_now_takes_the_upcoming_period_before_the_start(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    service = await make_service(client, owner, org)
    engagement = await engage(
        client, owner, org, customer, service, frequency="annually", start_date=d(60)
    )
    created = ok_data(
        await client.post(create_job_url(org, customer, engagement), headers=owner.headers), 201
    )
    assert created["period_start"] == d(60)


async def test_create_job_now_reports_why_it_cannot(client: AsyncClient) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    services = [
        await make_service(client, owner, org, code) for code in ("ZA1", "ZB1", "ZC1", "ZD1")
    ]
    no_start = await engage(client, owner, org, customer, services[0], frequency="monthly")
    inactive = await engage(
        client, owner, org, customer, services[1], frequency="monthly", start_date=d(-5),
        is_active=False,
    )  # fmt: skip
    ended = await engage(
        client, owner, org, customer, services[2], frequency="monthly", start_date=d(-90),
        end_date=d(-60),
    )  # fmt: skip
    archived = await engage(
        client, owner, org, customer, services[3], frequency="monthly", start_date=d(-5)
    )
    reasons = {}
    for key, engagement in (("no_start", no_start), ("inactive", inactive), ("ended", ended)):
        response = await client.post(
            create_job_url(org, customer, engagement), headers=owner.headers
        )
        assert response.status_code == 409, response.text
        assert error(response)["code"] == "ENGAGEMENT_NOT_SCHEDULABLE"
        reasons[key] = error(response)["message"]
    assert "start date" in reasons["no_start"]
    assert "inactive" in reasons["inactive"]
    assert "ended" in reasons["ended"]

    await sql("UPDATE clients SET status = 'archived' WHERE id = :c", c=str(customer.id))
    response = await client.post(create_job_url(org, customer, archived), headers=owner.headers)
    assert response.status_code == 409
    assert error(response)["code"] == "ENGAGEMENT_NOT_SCHEDULABLE"
    assert "client" in error(response)["message"].lower()


async def test_create_job_now_retries_a_failed_period_at_once(client: AsyncClient) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    service = await make_service(client, owner, org)
    engagement = await engage(
        client, owner, org, customer, service, frequency="monthly", start_date=d(-2),
        auto_create_jobs=True,
    )  # fmt: skip
    await sql("UPDATE clients SET status = 'archived' WHERE id = :c", c=str(customer.id))
    await run_worker()
    await sql("UPDATE clients SET status = 'active' WHERE id = :c", c=str(customer.id))
    ok_data(
        await client.post(create_job_url(org, customer, engagement), headers=owner.headers), 201
    )
    [run] = await runs_of(client, owner, org, customer, engagement)
    assert run["status"] == "created" and run["error"] is None


async def test_the_routes_need_permissions_and_stay_in_the_practice(client: AsyncClient) -> None:
    owner, org = await practice(client)
    other_owner, other_org = await practice(client, "Other Accountants")
    customer = await make_client(org)
    service = await make_service(client, owner, org)
    engagement = await engage(
        client, owner, org, customer, service, frequency="monthly", start_date=d(-2)
    )
    # client.update without job.create is not enough, and neither is the other way round.
    async with get_sessionmaker()() as session:
        role = Role(organization_id=uuid.UUID(org["id"]), name="Clients only")
        session.add(role)
        await session.flush()
        for code in ("organization.read", "client.read", "client.update"):
            permission = await session.scalar(select(Permission).where(Permission.code == code))
            assert permission is not None
            session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        await session.commit()
    editor = await staff(org, "Clients only")
    reader = await staff(org, "staff")
    denied = await client.post(create_job_url(org, customer, engagement), headers=editor.headers)
    assert denied.status_code == 403
    runs_url = org_url(org, f"/clients/{customer.id}/services/{engagement['id']}/job-runs")
    assert (await client.get(runs_url, headers=editor.headers)).status_code == 200
    assert (await client.get(runs_url, headers=reader.headers)).status_code in (200, 403)
    nobody = await client.get(runs_url)
    assert nobody.status_code == 401

    # Another practice cannot see the engagement through its own URL or ours.
    other_customer = await make_client(other_org)
    mixed = await client.post(
        org_url(other_org, f"/clients/{other_customer.id}/services/{engagement['id']}/create-job"),
        headers=other_owner.headers,
    )
    assert mixed.status_code == 404
    wrong_client = await client.post(
        org_url(
            org, f"/clients/{(await make_client(org)).id}/services/{engagement['id']}/create-job"
        ),
        headers=owner.headers,
    )
    assert wrong_client.status_code == 404
    foreign = await client.get(runs_url, headers=other_owner.headers)
    assert foreign.status_code == 404


async def test_the_job_runs_list_is_newest_first_and_capped(client: AsyncClient) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    service = await make_service(client, owner, org)
    engagement = await engage(
        client, owner, org, customer, service, frequency="monthly", start_date=d(-400)
    )
    rows = [
        (str(uuid.uuid4()), org["id"], engagement["id"], date(2020, 1, 1) + timedelta(days=31 * n))
        for n in range(55)
    ]
    for row_id, org_id, engagement_id, start in rows:
        await sql(
            "INSERT INTO engagement_job_runs (id, organization_id, client_service_id, "
            "period_start, status, error) VALUES (:i, :o, :e, :s, 'failed', 'x')",
            i=row_id, o=org_id, e=engagement_id, s=start,
        )  # fmt: skip
    listed = await runs_of(client, owner, org, customer, engagement)
    assert len(listed) == 50
    starts = [r["period_start"] for r in listed]
    assert starts == sorted(starts, reverse=True)
    assert set(listed[0]) == {"period_start", "period_end", "status", "error", "job", "created_at"}

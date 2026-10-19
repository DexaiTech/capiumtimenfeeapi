"""Copying a job: what comes along, what is validated, who may do it."""

import asyncio
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import update

from app.db.models import OrganizationMember
from app.db.session import get_sessionmaker
from tests.domain_factories import make_client, practice, service_id, staff
from tests.factories import audit_entries
from tests.job_helpers import add_task, alert_keys, create_job, job_url, log_on_job, set_job_row
from tests.time_helpers import err, ok_data, org_url


async def _copy(client: AsyncClient, actor, org, job_id: str, status: int = 201, **body):
    response = await client.post(job_url(org, job_id, "/copy"), headers=actor.headers, json=body)
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else response.json()["error"]


async def _tasks(client: AsyncClient, actor, org, job_id: str) -> list[dict]:
    return ok_data(await client.get(org_url(org, f"/jobs/{job_id}/tasks"), headers=actor.headers))


async def test_a_copy_takes_the_configuration_and_none_of_the_work(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    partner, manager, ann = (
        await staff(org, "partner"),
        await staff(org, "manager"),
        await staff(org),
    )
    accounts = await service_id(org, "ACCS")
    source = await create_job(
        client,
        owner,
        org,
        acme,
        "Accounts 2025",
        description="Year-end accounts",
        service_id=accounts,
        billing_basis="fixed",
        fixed_fee=120_000,
        hourly_rate=15_000,
        budget_minutes=600,
        budget_fee=100_000,
        period_start="2025-04-01",
        period_end="2026-03-31",
        start_date="2026-04-01",
        due_date="2026-12-31",
        partner_user_id=partner.id,
        manager_user_id=manager.id,
        assignee_user_ids=[ann.id],
        status="in_progress",
    )
    await log_on_job(client, owner, org, source, 120)
    await set_job_row(source["id"], status="completed", completed_at=datetime.now(UTC))

    copy = await _copy(client, owner, org, source["id"])
    assert copy["id"] != source["id"] and copy["number"] == "J00002"
    assert copy["name"] == "Copy of Accounts 2025" and copy["description"] == "Year-end accounts"
    assert (
        copy["status"] == "planned" and copy["completed_at"] is None and copy["closed_at"] is None
    )
    assert copy["client_id"] == str(acme.id) and copy["service_id"] == str(accounts)
    assert copy["billing_basis"] == "fixed" and copy["fixed_fee"] == 120_000
    assert copy["hourly_rate"] == 15_000
    assert (copy["budget_minutes"], copy["budget_fee"]) == (600, 100_000)
    assert (copy["period_start"], copy["period_end"]) == ("2025-04-01", "2026-03-31")
    assert (copy["start_date"], copy["due_date"]) == ("2026-04-01", "2026-12-31")
    assert copy["partner_user_id"] == str(partner.id) and copy["manager_user_id"] == str(manager.id)
    assert copy["assignee_user_ids"] == [str(ann.id)]
    # No time, no WIP, nothing billed: the copy starts clean.
    assert copy["logged_minutes"] == 0 and copy["wip_value"] == 0 and copy["time_value"] == 0
    assert copy["billed_value"] == 0 and copy["task_counts"] == {"total": 0, "completed": 0}
    # The source is untouched.
    still = ok_data(await client.get(job_url(org, source["id"]), headers=owner.headers))
    assert still["status"] == "completed" and still["logged_minutes"] == 120
    assert still["number"] == "J00001"

    entry = (await audit_entries(org["id"], "job.copied"))[0]
    assert entry["target_id"] == copy["id"]
    assert entry["metadata"]["source_job_id"] == source["id"]
    assert entry["metadata"]["source_number"] == "J00001"
    assert entry["metadata"]["source_name"] == "Accounts 2025"
    assert entry["metadata"]["tasks_copied"] == 0 and entry["metadata"]["client_name"] == "Acme Ltd"


async def test_a_copy_can_change_what_it_needs_to(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme, beta = await make_client(org, "Acme Ltd"), await make_client(org, "Beta plc")
    books = await service_id(org, "BOOKS")
    source = await create_job(
        client, owner, org, acme, "Accounts", service_id=await service_id(org, "ACCS"),
        start_date="2026-04-01", due_date="2026-12-31", billing_basis="hourly",
    )  # fmt: skip
    copy = await _copy(
        client,
        owner,
        org,
        source["id"],
        name="Beta accounts",
        client_id=str(beta.id),
        service_id=str(books),
        start_date="2027-04-01",
        due_date="2027-12-31",
    )
    assert copy["name"] == "Beta accounts" and copy["client_id"] == str(beta.id)
    assert copy["client_name"] == "Beta plc" and copy["service_id"] == str(books)
    assert (copy["start_date"], copy["due_date"]) == ("2027-04-01", "2027-12-31")
    # Null leaves a service and the dates empty instead of copying them.
    bare = await _copy(
        client, owner, org, source["id"], service_id=None, start_date=None, due_date=None
    )
    assert bare["service_id"] is None and bare["start_date"] is None and bare["due_date"] is None
    assert bare["name"] == "Copy of Accounts"
    # The long default name is cut to the 200 characters a name may have.
    long = await create_job(client, owner, org, acme, "x" * 200)
    cut = await _copy(client, owner, org, long["id"])
    assert len(cut["name"]) == 200 and cut["name"].startswith("Copy of x")


async def test_the_tasks_come_along_as_not_started(client: AsyncClient) -> None:
    owner, org = await practice(client)
    ann, bob = await staff(org, full_name="Ann Able"), await staff(org, full_name="Bob Baker")
    acme = await make_client(org)
    source = await create_job(
        client, owner, org, acme, "Accounts", start_date="2026-04-01", due_date="2026-12-31"
    )
    await add_task(
        client, owner, org, source, "Bookkeeping", description="Monthly", budget_minutes=300,
        due_date="2026-05-31", assignee_user_ids=[ann.id, bob.id], status="in_progress",
    )  # fmt: skip
    await add_task(client, owner, org, source, "Review", status="completed")
    await add_task(
        client, owner, org, source, "File", due_date="2026-12-01", assignee_user_ids=[bob.id]
    )

    copy = await _copy(client, owner, org, source["id"])
    copied = await _tasks(client, owner, org, copy["id"])
    assert [t["title"] for t in copied] == ["Bookkeeping", "Review", "File"]
    assert [t["sort_order"] for t in copied] == [0, 1, 2]
    assert {t["status"] for t in copied} == {"not_started"}
    assert all(t["completed_at"] is None and t["logged_minutes"] == 0 for t in copied)
    assert copied[0]["description"] == "Monthly" and copied[0]["budget_minutes"] == 300
    assert copied[0]["due_date"] == "2026-05-31"
    assert sorted(a["full_name"] for a in copied[0]["assignees"]) == ["Ann Able", "Bob Baker"]
    assert [a["full_name"] for a in copied[2]["assignees"]] == ["Bob Baker"]
    assert copy["task_counts"] == {"total": 3, "completed": 0}
    # The source's own tasks keep their state.
    original = await _tasks(client, owner, org, source["id"])
    assert [t["status"] for t in original] == ["in_progress", "completed", "not_started"]
    assert (await audit_entries(org["id"], "job.copied"))[0]["metadata"]["tasks_copied"] == 3

    # A task due outside the new job's dates keeps no due date; the others keep theirs.
    shifted = await _copy(
        client, owner, org, source["id"], start_date="2027-01-01", due_date="2027-12-31"
    )
    assert [t["due_date"] for t in await _tasks(client, owner, org, shifted["id"])] == [None] * 3
    narrower = await _copy(client, owner, org, source["id"], due_date="2026-06-30")
    assert [t["due_date"] for t in await _tasks(client, owner, org, narrower["id"])] == [
        "2026-05-31",
        None,
        None,
    ]


async def test_tasks_and_assignees_can_be_left_out(client: AsyncClient) -> None:
    owner, org = await practice(client)
    ann = await staff(org)
    acme = await make_client(org)
    source = await create_job(
        client, owner, org, acme, "Accounts", assignee_user_ids=[ann.id], manager_user_id=ann.id
    )
    await add_task(client, owner, org, source, "Draft", assignee_user_ids=[ann.id])
    without_tasks = await _copy(client, owner, org, source["id"], copy_tasks=False)
    assert without_tasks["task_counts"]["total"] == 0
    assert without_tasks["assignee_user_ids"] == [str(ann.id)]
    without_people = await _copy(client, owner, org, source["id"], copy_assignees=False)
    assert without_people["assignee_user_ids"] == []
    assert [a["assignees"] for a in await _tasks(client, owner, org, without_people["id"])] == [[]]
    # The manager is part of the job's configuration, not of "assignees".
    assert without_people["manager_user_id"] == str(ann.id)


async def test_people_who_left_are_not_copied(client: AsyncClient) -> None:
    owner, org = await practice(client)
    manager, partner, ann = (
        await staff(org, "manager"),
        await staff(org, "partner"),
        await staff(org),
    )
    acme = await make_client(org)
    source = await create_job(
        client, owner, org, acme, "Accounts", manager_user_id=manager.id,
        partner_user_id=partner.id, assignee_user_ids=[ann.id, manager.id],
    )  # fmt: skip
    await add_task(client, owner, org, source, "Draft", assignee_user_ids=[ann.id, manager.id])
    async with get_sessionmaker()() as session:
        await session.execute(
            update(OrganizationMember)
            .where(OrganizationMember.user_id.in_([manager.id, ann.id]))
            .values(status="removed")
        )
        await session.commit()
    copy = await _copy(client, owner, org, source["id"])
    assert copy["manager_user_id"] is None and copy["partner_user_id"] == str(partner.id)
    assert copy["assignee_user_ids"] == []
    assert [t["assignees"] for t in await _tasks(client, owner, org, copy["id"])] == [[]]


async def test_a_copy_is_validated_like_a_new_job(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org)
    gone = await make_client(org, status="archived")
    other_owner, other = await practice(client, "Other & Co")
    theirs = await make_client(other)
    their_service = await service_id(other, "ACCS")
    source = await create_job(client, owner, org, acme, "Accounts")
    sid = source["id"]
    assert (await _copy(client, owner, org, sid, 409, client_id=str(gone.id)))[
        "code"
    ] == "CLIENT_INACTIVE"
    await _copy(client, owner, org, sid, 404, client_id=str(theirs.id))
    await _copy(client, owner, org, sid, 404, service_id=str(their_service))
    await _copy(client, owner, org, sid, 404, service_id="00000000-0000-4000-8000-000000000000")
    clash = await _copy(
        client, owner, org, sid, 422, start_date="2026-12-31", due_date="2026-04-01"
    )
    assert clash["details"][0]["field"] == "due_date"
    for bad in (
        {"name": ""},
        {"name": None},
        {"client_id": None},
        {"copy_tasks": "maybe"},
        {"unknown": 1},
    ):
        await _copy(client, owner, org, sid, 422, **bad)
    # A closed job can be copied.
    await set_job_row(sid, status="closed")
    assert (await _copy(client, owner, org, sid))["status"] == "planned"
    # Another practice's job is not found, whoever asks.
    await _copy(client, other_owner, org, sid, 404)
    mine = await create_job(client, other_owner, other, theirs, "Theirs")
    await _copy(client, owner, org, mine["id"], 404)
    await _copy(client, owner, org, "00000000-0000-4000-8000-000000000000", 404)


async def test_who_may_copy(client: AsyncClient) -> None:
    owner, org = await practice(client)
    source = await create_job(client, owner, org, await make_client(org), "Accounts")
    for role in ("staff", "viewer", "billing"):
        person = await staff(org, role)
        error = err(
            await client.post(job_url(org, source["id"], "/copy"), headers=person.headers, json={}),
            403,
        )
        assert error["code"] == "PERMISSION_DENIED", role
    manager = await staff(org, "manager")
    assert (await _copy(client, manager, org, source["id"]))["number"] == "J00002"
    anonymous = await client.post(job_url(org, source["id"], "/copy"), json={})
    assert anonymous.status_code == 401


async def test_a_copy_does_not_inherit_budget_alerts(client: AsyncClient) -> None:
    owner, org = await practice(client)
    source = await create_job(
        client, owner, org, await make_client(org), "Accounts", budget_minutes=600, hourly_rate=6000
    )
    await log_on_job(client, owner, org, source, 600)
    assert await alert_keys(source["id"]) == [("minutes", 80), ("minutes", 100)]
    copy = await _copy(client, owner, org, source["id"])
    assert await alert_keys(copy["id"]) == []
    assert copy["budget_minutes_used_bp"] == 0


async def test_parallel_copies_take_distinct_numbers(client: AsyncClient) -> None:
    owner, org = await practice(client)
    source = await create_job(client, owner, org, await make_client(org), "Accounts")
    await add_task(client, owner, org, source, "Draft")
    copies = await asyncio.gather(*(_copy(client, owner, org, source["id"]) for _ in range(5)))
    assert sorted(c["number"] for c in copies) == [f"J{n:05d}" for n in range(2, 7)]
    assert all(c["task_counts"]["total"] == 1 for c in copies)

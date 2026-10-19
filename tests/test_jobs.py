"""Jobs: creation defaults, numbering, status workflow, assignees, actuals, access."""

import asyncio
import uuid
from datetime import date

from httpx import AsyncClient

from tests.domain_factories import (
    make_client,
    make_disbursement,
    make_job,
    make_time_entry,
    practice,
    service_id,
    staff,
)
from tests.factories import audit_entries
from tests.time_helpers import bill_items, err, ok_data, org_url, write_off_items


async def _create(client: AsyncClient, actor, org, expect: int = 201, **body):
    response = await client.post(org_url(org, "/jobs"), headers=actor.headers, json=body)
    assert response.status_code == expect, response.text
    return response.json()["data"] if expect < 300 else response.json()["error"]


async def test_create_job_numbers_and_defaults_from_service(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    accounts = await service_id(org, "ACCS")  # fixed, default fee 120000
    job = await _create(
        client, owner, org, client_id=str(acme.id), name="Accounts 2026", service_id=str(accounts)
    )
    assert job["number"] == "J00001"
    assert job["billing_basis"] == "fixed"
    assert job["fixed_fee"] == 120_000
    assert job["status"] == "planned"
    assert job["client_name"] == "Acme Ltd"
    assert job["service_name"] == "Year-end accounts"
    assert job["logged_minutes"] == 0 and job["wip_value"] == 0
    assert job["budget_minutes_used_bp"] is None

    second = await _create(client, owner, org, client_id=str(acme.id), name="Advice")
    assert second["number"] == "J00002"
    assert second["billing_basis"] == "hourly"
    assert second["fixed_fee"] is None

    explicit = await _create(
        client,
        owner,
        org,
        client_id=str(acme.id),
        name="Override",
        service_id=str(accounts),
        billing_basis="hourly",
    )
    assert explicit["billing_basis"] == "hourly" and explicit["fixed_fee"] is None

    entries = await audit_entries(org["id"], "job.created")
    assert len(entries) == 3
    assert entries[0]["metadata"]["number"] == "J00001"
    assert entries[0]["metadata"]["client_name"] == "Acme Ltd"


async def test_parallel_creates_take_distinct_numbers(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org)
    results = await asyncio.gather(
        *(_create(client, owner, org, client_id=str(acme.id), name=f"Job {i}") for i in range(6))
    )
    assert sorted(r["number"] for r in results) == [f"J{n:05d}" for n in range(1, 7)]


async def test_fixed_job_needs_fee_and_validation(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org)
    books = await service_id(org, "BOOKS")  # hourly, no fee
    error = await _create(
        client,
        owner,
        org,
        422,
        client_id=str(acme.id),
        name="X",
        service_id=str(books),
        billing_basis="fixed",
    )
    assert error["details"][0]["field"] == "fixed_fee"
    # Field validation
    await _create(client, owner, org, 422, client_id=str(acme.id), name="")
    await _create(client, owner, org, 422, client_id=str(acme.id), name="X", fixed_fee=-1)
    await _create(client, owner, org, 422, client_id=str(acme.id), name="X", unknown=1)
    await _create(
        client,
        owner,
        org,
        422,
        client_id=str(acme.id),
        name="X",
        period_start="2026-04-01",
        period_end="2026-03-31",
    )
    await _create(client, owner, org, 422, client_id=str(acme.id), name="X", status="completed")


async def test_create_job_rules_on_client_and_people(client: AsyncClient) -> None:
    owner, org = await practice(client)
    gone = await make_client(org, status="archived")
    inactive = await make_client(org, status="inactive")
    prospect = await make_client(org, status="prospect")
    for c in (gone, inactive):
        error = await _create(client, owner, org, 409, client_id=str(c.id), name="X")
        assert error["code"] == "CLIENT_INACTIVE"
    await _create(client, owner, org, client_id=str(prospect.id), name="Proposal")
    await _create(client, owner, org, 404, client_id=str(uuid.uuid4()), name="X")

    removed = await staff(org, status="removed")
    error = await _create(
        client,
        owner,
        org,
        422,
        client_id=str(prospect.id),
        name="X",
        manager_user_id=str(removed.id),
    )
    assert error["details"][0]["field"] == "manager_user_id"
    error = await _create(
        client,
        owner,
        org,
        422,
        client_id=str(prospect.id),
        name="X",
        assignee_user_ids=[str(removed.id)],
    )
    assert error["details"][0]["field"] == "user_ids"


async def test_other_practice_ids_are_not_found(client: AsyncClient) -> None:
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other & Co")
    theirs = await make_client(other)
    their_job = await make_job(other, theirs)
    await _create(client, owner, org, 404, client_id=str(theirs.id), name="X")
    their_service = await service_id(other, "ACCS")
    mine = await make_client(org)
    await _create(
        client, owner, org, 404, client_id=str(mine.id), name="X", service_id=str(their_service)
    )
    url = org_url(org, f"/jobs/{their_job.id}")
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    assert (await client.patch(url, headers=owner.headers, json={"name": "Y"})).status_code == 404
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    response = await client.post(f"{url}/status", headers=owner.headers, json={"status": "on_hold"})
    assert response.status_code == 404
    # And the other practice's owner cannot reach ours at all.
    assert (await client.get(org_url(org, "/jobs"), headers=other_owner.headers)).status_code == 404


async def test_permissions(client: AsyncClient) -> None:
    _, org = await practice(client)
    acme = await make_client(org)
    job = await make_job(org, acme)
    person = await staff(org, "staff")
    manager = await staff(org, "manager")
    url = org_url(org, f"/jobs/{job.id}")
    # Staff reads, but cannot create, update or delete.
    assert (await client.get(org_url(org, "/jobs"), headers=person.headers)).status_code == 200
    assert (await client.get(url, headers=person.headers)).status_code == 200
    await _create(client, person, org, 403, client_id=str(acme.id), name="X")
    assert (await client.patch(url, headers=person.headers, json={"name": "Y"})).status_code == 403
    response = await client.post(
        f"{url}/status", headers=person.headers, json={"status": "on_hold"}
    )
    assert response.status_code == 403
    response = await client.put(f"{url}/assignees", headers=person.headers, json={"user_ids": []})
    assert response.status_code == 403
    assert (await client.delete(url, headers=person.headers)).status_code == 403
    # Managers create and update but do not delete.
    await _create(client, manager, org, client_id=str(acme.id), name="By manager")
    assert (await client.patch(url, headers=manager.headers, json={"name": "Y"})).status_code == 200
    assert (await client.delete(url, headers=manager.headers)).status_code == 403
    # A role without job.read at all.
    custom = await _role_without(org, "job.read")
    assert (await client.get(org_url(org, "/jobs"), headers=custom.headers)).status_code == 403


async def _role_without(org: dict, code: str):
    """A member of a custom role holding nothing but organization.read."""
    from sqlalchemy import select

    from app.db.models import Permission, Role, RolePermission
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        role = Role(organization_id=uuid.UUID(org["id"]), name=f"No {code}")
        session.add(role)
        await session.flush()
        perm = await session.scalar(
            select(Permission).where(Permission.code == "organization.read")
        )
        assert perm is not None
        session.add(RolePermission(role_id=role.id, permission_id=perm.id))
        await session.commit()
        name = role.name
    return await staff(org, name)


async def test_update_job(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org)
    job = await _create(client, owner, org, client_id=str(acme.id), name="Accounts")
    url = org_url(org, f"/jobs/{job['id']}")
    data = ok_data(
        await client.patch(
            url,
            headers=owner.headers,
            json={
                "name": "Accounts FY26",
                "budget_minutes": 600,
                "budget_fee": 100_000,
                "due_date": "2026-12-31",
                "hourly_rate": 15_000,
            },
        )
    )
    assert data["name"] == "Accounts FY26" and data["hourly_rate"] == 15_000
    assert data["budget_minutes_used_bp"] == 0
    # Nulls only where the column is nullable; client and status are not patchable.
    assert (await client.patch(url, headers=owner.headers, json={"name": None})).status_code == 422
    response = await client.patch(url, headers=owner.headers, json={"client_id": str(acme.id)})
    assert response.status_code == 422
    response = await client.patch(url, headers=owner.headers, json={"status": "completed"})
    assert response.status_code == 422
    error = err(
        await client.patch(url, headers=owner.headers, json={"billing_basis": "fixed"}), 422
    )
    assert error["details"][0]["field"] == "fixed_fee"
    response = await client.patch(
        url, headers=owner.headers, json={"period_start": "2026-04-01", "period_end": "2026-01-01"}
    )
    assert response.status_code == 422
    changes = (await audit_entries(org["id"], "job.updated"))[0]["metadata"]["changes"]
    assert changes["name"] == ["Accounts", "Accounts FY26"]
    assert changes["due_date"] == [None, "2026-12-31"]


async def test_status_transitions(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org)
    job = await _create(client, owner, org, client_id=str(acme.id), name="Accounts")
    url = org_url(org, f"/jobs/{job['id']}/status")

    async def move(target: str, expect: int = 200) -> dict:
        response = await client.post(url, headers=owner.headers, json={"status": target})
        assert response.status_code == expect, response.text
        return response.json()["data"] if expect == 200 else response.json()["error"]

    assert (await move("completed", 409))["code"] == "INVALID_STATUS_TRANSITION"
    assert (await move("planned", 409))["code"] == "INVALID_STATUS_TRANSITION"
    await move("on_hold")  # a job can be put on hold before it starts
    await move("in_progress")
    await move("on_hold")
    await move("completed", 409)
    await move("in_progress")
    done = await move("completed")
    assert done["completed_at"] is not None
    await move("cancelled", 409)
    reopened = await move("in_progress")
    assert reopened["completed_at"] is None
    await move("cancelled")
    for target in ("planned", "in_progress", "on_hold", "completed"):
        await move(target, 409)
    assert await move("bogus", 422)
    log = await audit_entries(org["id"], "job.status_changed")
    assert [e["metadata"]["new"] for e in log] == [
        "on_hold",
        "in_progress",
        "on_hold",
        "in_progress",
        "completed",
        "in_progress",
        "cancelled",
    ]


async def test_assignees(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org)
    a, b = await staff(org), await staff(org)
    suspended = await staff(org, status="suspended")
    job = await _create(
        client, owner, org, client_id=str(acme.id), name="Accounts", assignee_user_ids=[str(a.id)]
    )
    assert job["assignee_user_ids"] == [str(a.id)]
    url = org_url(org, f"/jobs/{job['id']}/assignees")
    data = ok_data(
        await client.put(
            url, headers=owner.headers, json={"user_ids": [str(b.id), str(a.id), str(b.id)]}
        )
    )
    assert sorted(data["assignee_user_ids"]) == sorted([str(a.id), str(b.id)])
    error = err(
        await client.put(url, headers=owner.headers, json={"user_ids": [str(suspended.id)]}), 422
    )
    assert error["details"][0]["user_ids"] == [str(suspended.id)]
    data = ok_data(await client.put(url, headers=owner.headers, json={"user_ids": [str(b.id)]}))
    assert data["assignee_user_ids"] == [str(b.id)]
    data = ok_data(await client.put(url, headers=owner.headers, json={"user_ids": []}))
    assert data["assignee_user_ids"] == []
    assert len(await audit_entries(org["id"], "job.assignees_changed")) == 3


async def test_list_filters_sort_and_pagination(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    beta = await make_client(org, "Beta plc")
    person = await staff(org)
    manager = await staff(org, "manager")
    vat = await service_id(org, "VAT")
    jobs = [
        await _create(
            client,
            owner,
            org,
            client_id=str(acme.id),
            name="Accounts",
            due_date="2026-12-01",
            manager_user_id=str(manager.id),
        ),
        await _create(
            client,
            owner,
            org,
            client_id=str(acme.id),
            name="VAT Q1",
            due_date="2026-10-01",
            service_id=str(vat),
            assignee_user_ids=[str(person.id)],
        ),
        await _create(
            client,
            owner,
            org,
            client_id=str(beta.id),
            name="Payroll",
            status="in_progress",
            partner_user_id=str(owner.id),
        ),
        await _create(
            client,
            owner,
            org,
            client_id=str(beta.id),
            name="VAT Q2",
            due_date="2026-11-01",
            service_id=str(vat),
        ),
    ]
    base = org_url(org, "/jobs")

    async def names(**params) -> list[str]:
        data = ok_data(await client.get(base, headers=person.headers, params=params))
        return [j["name"] for j in data]

    assert await names() == ["VAT Q2", "Payroll", "VAT Q1", "Accounts"]
    assert await names(sort="due_date") == ["VAT Q1", "VAT Q2", "Accounts", "Payroll"]
    assert await names(sort="-due_date") == ["Payroll", "Accounts", "VAT Q2", "VAT Q1"]
    assert await names(sort="number") == ["Accounts", "VAT Q1", "Payroll", "VAT Q2"]
    assert await names(client_id=str(beta.id)) == ["VAT Q2", "Payroll"]
    assert await names(status="in_progress") == ["Payroll"]
    assert sorted(await names(status=["in_progress", "planned"])) == sorted(j["name"] for j in jobs)
    assert await names(manager_user_id=str(manager.id)) == ["Accounts"]
    assert await names(partner_user_id=str(owner.id)) == ["Payroll"]
    assert await names(assignee="me") == ["VAT Q1"]
    assert await names(assignee=str(person.id)) == ["VAT Q1"]
    assert await names(service_id=str(vat)) == ["VAT Q2", "VAT Q1"]
    assert await names(due_from="2026-10-15", due_to="2026-11-30") == ["VAT Q2"]
    assert await names(q="vat") == ["VAT Q2", "VAT Q1"]
    assert await names(q="beta") == ["VAT Q2", "Payroll"]
    assert await names(q="J00003") == ["Payroll"]
    response = await client.get(base, headers=person.headers, params={"assignee": "nobody"})
    assert response.status_code == 422
    response = await client.get(base, headers=person.headers, params={"sort": "name"})
    assert response.status_code == 422

    # Keyset pagination over every sort, including the nullable due date.
    for sort in ("due_date", "-due_date", "number", "-number", "created_at", "-created_at"):
        expected = await names(sort=sort)
        seen: list[str] = []
        cursor = None
        while True:
            params = {"sort": sort, "limit": 1, **({"cursor": cursor} if cursor else {})}
            body = (await client.get(base, headers=person.headers, params=params)).json()
            seen += [j["name"] for j in body["data"]]
            cursor = body["meta"]["next_cursor"]
            if not cursor:
                break
        assert seen == expected, sort
    response = await client.get(base, headers=person.headers, params={"cursor": "garbage"})
    assert response.status_code == 422


async def test_actuals(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org)
    job = await make_job(org, acme, budget_minutes=600, budget_fee=50_000)
    other_job = await make_job(org, acme)
    person = await staff(org)
    wip = await make_time_entry(org, person, client=acme, job=job, minutes=90, charge_rate=10_000)
    billed = await make_time_entry(
        org, person, client=acme, job=job, minutes=60, charge_rate=10_000, billing_status="billed"
    )
    written = await make_time_entry(
        org,
        person,
        client=acme,
        job=job,
        minutes=30,
        charge_rate=10_000,
        billing_status="written_off",
    )
    await make_time_entry(
        org, person, client=acme, job=job, minutes=45, is_billable=False, charge_rate=10_000
    )
    drafted = await make_time_entry(
        org, person, client=acme, job=job, minutes=60, charge_rate=10_000, billing_status="in_draft"
    )
    await make_time_entry(org, person, client=acme, job=other_job, minutes=60)
    disb = await make_disbursement(org, acme, 1_300, job=job, billing_status="billed")

    await bill_items(
        org, acme.id, [("time", billed.id, 10_000, 12_000), ("disbursement", disb.id, 1_300, 1_300)]
    )
    await bill_items(org, acme.id, [("time", drafted.id, 10_000, 9_000)], invoice_status="draft")
    # A reversed allocation (voided invoice) does not count.
    await bill_items(
        org, acme.id, [("time", wip.id, 15_000, 15_000)], invoice_status="void", reversed_=True
    )
    await write_off_items(org, acme.id, [("time", written.id, 5_000)])

    data = ok_data(await client.get(org_url(org, f"/jobs/{job.id}"), headers=owner.headers))
    assert data["logged_minutes"] == 90 + 60 + 30 + 45 + 60
    assert data["billable_minutes"] == 90 + 60 + 30 + 60
    assert data["time_value"] == 15_000 + 10_000 + 5_000 + 10_000
    assert data["wip_value"] == 15_000  # unbilled billable only
    assert data["billed_value"] == 12_000 + 1_300  # issued invoices, live allocations
    assert data["written_off_value"] == 5_000
    assert data["budget_minutes_used_bp"] == 4_750  # 285 / 600
    assert data["budget_fee_used_bp"] == 8_000  # 40000 / 50000

    listed = ok_data(await client.get(org_url(org, "/jobs"), headers=owner.headers))
    by_id = {j["id"]: j for j in listed}
    assert by_id[str(job.id)]["billed_value"] == 13_300
    assert by_id[str(other_job.id)]["logged_minutes"] == 60


async def test_delete_job(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org)
    empty = await _create(client, owner, org, client_id=str(acme.id), name="Empty")
    used = await make_job(org, acme)
    with_disb = await make_job(org, acme)
    await make_time_entry(org, owner, client=acme, job=used)
    await make_disbursement(org, acme, job=with_disb)

    # A timer on the job is discarded with it.
    started = await client.post(
        org_url(org, "/timer/start"), headers=owner.headers, json={"job_id": empty["id"]}
    )
    assert started.status_code == 201, started.text
    response = await client.delete(org_url(org, f"/jobs/{empty['id']}"), headers=owner.headers)
    assert response.status_code == 200, response.text
    assert (
        await client.get(org_url(org, f"/jobs/{empty['id']}"), headers=owner.headers)
    ).status_code == 404
    assert ok_data(await client.get(org_url(org, "/timer"), headers=owner.headers)) is None
    assert (
        await client.delete(org_url(org, f"/jobs/{empty['id']}"), headers=owner.headers)
    ).status_code == 404
    for job in (used, with_disb):
        error = err(
            await client.delete(org_url(org, f"/jobs/{job.id}"), headers=owner.headers), 409
        )
        assert error["code"] == "JOB_IN_USE"
    names = [
        j["name"] for j in ok_data(await client.get(org_url(org, "/jobs"), headers=owner.headers))
    ]
    assert "Empty" not in names
    assert (await audit_entries(org["id"], "job.deleted"))[0]["metadata"]["name"] == "Empty"
    # The number is not reused.
    again = await _create(client, owner, org, client_id=str(acme.id), name="Next")
    assert again["number"] == "J00002"
    assert date.fromisoformat(again["created_at"][:10])

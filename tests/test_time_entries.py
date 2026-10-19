"""Time entries: defaults, rounding, locks, the daily cap, access, the weekly grid, bulk."""

import asyncio
import uuid
from datetime import date, timedelta

from httpx import AsyncClient
from sqlalchemy import select, update

from app.db.models import ActivityCode, Job
from app.db.session import get_sessionmaker
from tests.domain_factories import (
    activity_code_id,
    make_client,
    make_job,
    make_staff_rate,
    practice,
    staff,
)
from tests.factories import audit_entries
from tests.time_helpers import (
    MONDAY,
    entry_row,
    err,
    log_time,
    ok_data,
    org_url,
    set_entry,
    set_manager,
    set_practice,
    set_sheet,
    sheet_count,
)


async def test_log_time_happy_path(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    job = await make_job(org, acme, "Accounts")
    person = await staff(org)
    await make_staff_rate(org, person, 9_000, 3_000)
    acc = await activity_code_id(org, "ACC")
    entry = await log_time(
        client,
        person,
        org,
        job_id=job.id,
        activity_code_id=acc,
        minutes=90,
        description="Draft accounts",
    )
    assert entry["client_id"] == str(acme.id)  # implied by the job
    assert entry["client_name"] == "Acme Ltd" and entry["job_name"] == "Accounts"
    assert entry["activity_code"] == "ACC"
    assert entry["is_billable"] is True
    assert entry["charge_rate"] == 9_000 and entry["charge_value"] == 13_500
    assert entry["cost_rate"] is None and entry["cost_value"] is None  # staff lack rate.read
    assert entry["billing_status"] == "unbilled"
    assert entry["timesheet_status"] == "open" and entry["is_locked"] is False
    assert entry["user_id"] == str(person.id) and entry["created_by"] == str(person.id)
    # Owners see cost.
    seen = ok_data(
        await client.get(org_url(org, f"/time-entries/{entry['id']}"), headers=owner.headers)
    )
    assert seen["cost_rate"] == 3_000 and seen["cost_value"] == 4_500
    log = await audit_entries(org["id"], "time_entry.created")
    assert log[0]["target_id"] == entry["id"]
    assert log[0]["metadata"]["charge_value"] == 13_500
    assert log[0]["metadata"]["client_name"] == "Acme Ltd"


async def test_timesheet_created_on_demand_per_week(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    a = await log_time(client, person, org, entry_date=MONDAY.isoformat())
    b = await log_time(client, person, org, entry_date=(MONDAY + timedelta(days=6)).isoformat())
    c = await log_time(client, person, org, entry_date=(MONDAY + timedelta(days=7)).isoformat())
    assert a["timesheet_id"] == b["timesheet_id"] != c["timesheet_id"]
    assert await sheet_count(org) == 2
    # A practice whose week starts on Sunday (7).
    _, sunday_org = await practice(client, "Sunday & Co")
    await set_practice(sunday_org, week_start=7)
    other = await staff(sunday_org)
    saturday = await log_time(client, other, sunday_org, entry_date="2026-09-12")
    sunday = await log_time(client, other, sunday_org, entry_date="2026-09-13")
    assert saturday["timesheet_id"] != sunday["timesheet_id"]
    sheet = ok_data(
        await client.get(
            org_url(sunday_org, f"/timesheets/{sunday['timesheet_id']}"), headers=other.headers
        )
    )
    assert sheet["week_start"] == "2026-09-13"


async def test_parallel_first_entries_share_one_sheet(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    results = await asyncio.gather(*(log_time(client, person, org, minutes=30) for _ in range(5)))
    assert len({r["timesheet_id"] for r in results}) == 1
    assert await sheet_count(org) == 1


async def test_minutes_round_up_to_increment(client: AsyncClient) -> None:
    _, org = await practice(client)
    await set_practice(org, time_increment_minutes=6)
    person = await staff(org)
    await make_staff_rate(org, person, 10_000)
    acme = await make_client(org)
    for day, (given, expected) in enumerate(((1, 6), (6, 6), (7, 12), (61, 66), (1440, 1440))):
        entry = await log_time(
            client,
            person,
            org,
            client_id=acme.id,
            minutes=given,
            entry_date=(MONDAY + timedelta(days=day)).isoformat(),
        )
        assert entry["minutes"] == expected
    await set_practice(org, time_increment_minutes=15)
    entry = await log_time(
        client, person, org, client_id=acme.id, minutes=1, entry_date="2026-09-20"
    )
    assert entry["minutes"] == 15
    assert entry["charge_value"] == 2_500
    for bad in (0, -5, 1441, "x"):
        await log_time(client, person, org, status=422, minutes=bad)


async def test_value_rounds_half_up(client: AsyncClient) -> None:
    _, org = await practice(client)
    await set_practice(org, time_increment_minutes=1)
    person = await staff(org)
    await make_staff_rate(org, person, 10_001)
    acme = await make_client(org)
    # 10001 * 7 / 60 = 1166.78 -> 1167;  10001 * 3 / 60 = 500.05 -> 500; 9 -> 1500.15 -> 1500
    expectations = {7: 1_167, 3: 500, 9: 1_500}
    await make_staff_rate(org, person, 10_002, effective_from=date(2026, 9, 1))
    # 10002 * 3 / 60 = 500.1 -> 500; 10002 * 5 / 60 = 833.5 -> 834 (half up)
    expectations = {3: 500, 5: 834, 7: 1_167}
    for minutes, value in expectations.items():
        entry = await log_time(client, person, org, client_id=acme.id, minutes=minutes)
        assert entry["charge_rate"] == 10_002
        assert entry["charge_value"] == value, minutes


async def test_billable_defaults_and_rules(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    hourly = await make_job(org, acme)
    internal = await make_job(org, acme, billing_basis="non_billable")
    admin = await activity_code_id(org, "ADM")  # non-chargeable
    acc = await activity_code_id(org, "ACC")
    assert (await log_time(client, person, org))["is_billable"] is False  # no client
    assert (await log_time(client, person, org, client_id=acme.id))["is_billable"] is True
    assert (await log_time(client, person, org, job_id=hourly.id))["is_billable"] is True
    assert (await log_time(client, person, org, job_id=internal.id))["is_billable"] is False
    assert (await log_time(client, person, org, job_id=hourly.id, activity_code_id=admin))[
        "is_billable"
    ] is False
    assert (await log_time(client, person, org, job_id=hourly.id, activity_code_id=acc))[
        "is_billable"
    ] is True
    # Explicit choices
    assert (await log_time(client, person, org, job_id=hourly.id, is_billable=False))[
        "is_billable"
    ] is False
    assert (
        await log_time(
            client, person, org, job_id=hourly.id, activity_code_id=admin, is_billable=True
        )
    )["is_billable"] is True
    error = await log_time(client, person, org, status=422, is_billable=True)
    assert error["details"][0]["field"] == "is_billable"
    error = await log_time(client, person, org, status=422, job_id=internal.id, is_billable=True)
    assert error["details"][0]["field"] == "is_billable"


async def test_references_are_checked(client: AsyncClient) -> None:
    _, org = await practice(client)
    _, other = await practice(client, "Other & Co")
    person = await staff(org)
    acme = await make_client(org)
    beta = await make_client(org)
    beta_job = await make_job(org, beta)
    theirs = await make_client(other)
    their_job = await make_job(other, theirs)
    their_code = await activity_code_id(other, "ACC")
    error = await log_time(client, person, org, status=422, client_id=acme.id, job_id=beta_job.id)
    assert error["details"][0]["field"] == "job_id"
    assert (await log_time(client, person, org, status=404, client_id=theirs.id))[
        "code"
    ] == "RESOURCE_NOT_FOUND"
    await log_time(client, person, org, status=404, job_id=their_job.id)
    await log_time(client, person, org, status=404, activity_code_id=their_code)
    await log_time(client, person, org, status=404, client_id=uuid.uuid4())
    # Inactive activity codes cannot be used for new time.
    code = await activity_code_id(org, "BKP")
    async with get_sessionmaker()() as session:
        await session.execute(
            update(ActivityCode).where(ActivityCode.id == code).values(is_active=False)
        )
        await session.commit()
    error = await log_time(client, person, org, status=422, activity_code_id=code)
    assert error["details"][0]["field"] == "activity_code_id"


async def test_client_inactive_and_job_closed(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    archived = await make_client(org, status="archived")
    inactive = await make_client(org, status="inactive")
    prospect = await make_client(org, status="prospect")
    acme = await make_client(org)
    for c in (archived, inactive):
        assert (await log_time(client, person, org, status=409, client_id=c.id))[
            "code"
        ] == "CLIENT_INACTIVE"
    await log_time(client, person, org, client_id=prospect.id)
    for job_status in ("completed", "cancelled"):
        job = await make_job(org, acme, status=job_status)
        assert (await log_time(client, person, org, status=409, job_id=job.id))[
            "code"
        ] == "JOB_CLOSED"
    for job_status in ("planned", "in_progress", "on_hold"):
        job = await make_job(org, acme, status=job_status)
        await log_time(client, person, org, job_id=job.id)

    # Editing: moving time onto a closed job or changing its minutes there is refused,
    # fixing its description is not.
    open_job = await make_job(org, acme)
    entry = await log_time(client, person, org, job_id=open_job.id)
    closed = await make_job(org, acme, status="completed")
    url = org_url(org, f"/time-entries/{entry['id']}")
    response = await client.patch(url, headers=person.headers, json={"job_id": str(closed.id)})
    assert err(response, 409)["code"] == "JOB_CLOSED"
    async with get_sessionmaker()() as session:
        await session.execute(update(Job).where(Job.id == open_job.id).values(status="completed"))
        await session.commit()
    response = await client.patch(url, headers=person.headers, json={"minutes": 30})
    assert err(response, 409)["code"] == "JOB_CLOSED"
    response = await client.patch(url, headers=person.headers, json={"description": "Fixed typo"})
    assert response.status_code == 200, response.text


async def test_day_cap(client: AsyncClient) -> None:
    _, org = await practice(client)
    await set_practice(org, time_increment_minutes=1)
    person = await staff(org)
    await log_time(client, person, org, minutes=1000)
    error = await log_time(client, person, org, status=422, minutes=441)
    assert error["code"] == "DAY_LIMIT_EXCEEDED"
    assert error["details"][0]["field"] == "minutes"
    last = await log_time(client, person, org, minutes=440)
    # Another day, another person are separate.
    await log_time(client, person, org, minutes=1440, entry_date="2026-09-08")
    other = await staff(org)
    await log_time(client, other, org, minutes=1440)
    # Editing counts the other entries but not itself.
    url = org_url(org, f"/time-entries/{last['id']}")
    assert (
        await client.patch(url, headers=person.headers, json={"minutes": 441})
    ).status_code == 422
    assert (
        await client.patch(url, headers=person.headers, json={"minutes": 400})
    ).status_code == 200
    response = await client.patch(url, headers=person.headers, json={"entry_date": "2026-09-08"})
    assert err(response, 422)["code"] == "DAY_LIMIT_EXCEEDED"
    # Rounding is applied before the cap.
    await set_practice(org, time_increment_minutes=60)
    fresh = await staff(org)
    assert (await log_time(client, fresh, org, minutes=1381))["minutes"] == 1440
    assert (await log_time(client, fresh, org, status=422, minutes=1))[
        "code"
    ] == "DAY_LIMIT_EXCEEDED"


async def test_parallel_entries_cannot_exceed_day_cap(client: AsyncClient) -> None:
    _, org = await practice(client)
    await set_practice(org, time_increment_minutes=1)
    person = await staff(org)
    responses = await asyncio.gather(
        *(
            client.post(
                org_url(org, "/time-entries"),
                headers=person.headers,
                json={"entry_date": MONDAY.isoformat(), "minutes": 500},
            )
            for _ in range(4)
        )
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 201, 422, 422]
    listed = ok_data(await client.get(org_url(org, "/time-entries"), headers=person.headers))
    assert sum(e["minutes"] for e in listed) == 1000


async def test_lock_rules(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    entry = await log_time(client, person, org, client_id=acme.id)
    url = org_url(org, f"/time-entries/{entry['id']}")
    for sheet_status in ("submitted", "approved"):
        await set_sheet(entry["timesheet_id"], status=sheet_status)
        assert (
            err(await client.patch(url, headers=person.headers, json={"minutes": 30}), 409)["code"]
            == "TIME_LOCKED"
        )
        assert err(await client.delete(url, headers=person.headers), 409)["code"] == "TIME_LOCKED"
        assert (await log_time(client, person, org, status=409))["code"] == "TIME_LOCKED"
        seen = ok_data(await client.get(url, headers=person.headers))
        assert seen["is_locked"] is True
    await set_sheet(entry["timesheet_id"], status="rejected")
    assert (
        await client.patch(url, headers=person.headers, json={"minutes": 30})
    ).status_code == 200
    await log_time(client, person, org)
    await set_sheet(entry["timesheet_id"], status="open")
    for billing_status in ("in_draft", "billed", "written_off"):
        await set_entry(entry["id"], billing_status=billing_status)
        assert (
            err(await client.patch(url, headers=person.headers, json={"minutes": 45}), 409)["code"]
            == "TIME_LOCKED"
        )
        assert err(await client.delete(url, headers=person.headers), 409)["code"] == "TIME_LOCKED"
    await set_entry(entry["id"], billing_status="unbilled")
    # Moving into a locked week is refused too.
    locked_week = await log_time(client, person, org, entry_date="2026-09-14")
    await set_sheet(locked_week["timesheet_id"], status="approved")
    response = await client.patch(url, headers=person.headers, json={"entry_date": "2026-09-15"})
    assert err(response, 409)["code"] == "TIME_LOCKED"


async def test_update_reprices_and_moves_week(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    await make_staff_rate(org, person, 6_000, 2_000)
    await make_staff_rate(org, person, 9_000, 3_000, effective_from=date(2026, 9, 14))
    acme = await make_client(org)
    job = await make_job(org, acme, hourly_rate=20_000)
    entry = await log_time(client, person, org, client_id=acme.id, minutes=60)
    assert entry["charge_rate"] == 6_000
    url = org_url(org, f"/time-entries/{entry['id']}")
    moved = ok_data(
        await client.patch(
            url, headers=person.headers, json={"entry_date": "2026-09-14", "minutes": 30}
        )
    )
    assert moved["charge_rate"] == 9_000 and moved["charge_value"] == 4_500
    assert moved["timesheet_id"] != entry["timesheet_id"]
    on_job = ok_data(await client.patch(url, headers=person.headers, json={"job_id": str(job.id)}))
    assert on_job["charge_rate"] == 20_000 and on_job["client_id"] == str(acme.id)
    row = await entry_row(entry["id"])
    assert row.cost_rate == 3_000 and row.cost_value == 1_500
    # Clearing the job keeps the client; clearing both makes it non-billable by default.
    cleared = ok_data(await client.patch(url, headers=person.headers, json={"job_id": None}))
    assert cleared["client_id"] == str(acme.id) and cleared["job_id"] is None
    assert cleared["charge_rate"] == 9_000
    none = ok_data(await client.patch(url, headers=person.headers, json={"client_id": None}))
    assert none["client_id"] is None and none["is_billable"] is False
    response = await client.patch(url, headers=person.headers, json={"is_billable": True})
    assert err(response, 422)["details"][0]["field"] == "is_billable"
    assert (
        await client.patch(url, headers=person.headers, json={"minutes": None})
    ).status_code == 422
    assert (
        await client.patch(url, headers=person.headers, json={"user_id": str(owner.id)})
    ).status_code == 422
    log = await audit_entries(org["id"], "time_entry.updated")
    assert log[0]["metadata"]["changes"]["entry_date"] == ["2026-09-07", "2026-09-14"]
    assert log[0]["metadata"]["changes"]["charge_rate"] == [6_000, 9_000]


async def test_delete_entry(client: AsyncClient) -> None:
    _, org = await practice(client)
    await set_practice(org, time_increment_minutes=1)
    person = await staff(org)
    entry = await log_time(client, person, org, minutes=25)
    url = org_url(org, f"/time-entries/{entry['id']}")
    assert (await client.delete(url, headers=person.headers)).status_code == 200
    assert (await client.get(url, headers=person.headers)).status_code == 404
    assert (await client.delete(url, headers=person.headers)).status_code == 404
    deleted = await audit_entries(org["id"], "time_entry.deleted")
    assert deleted[0]["metadata"]["minutes"] == 25


async def test_permissions(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    colleague = await staff(org)
    manager = await staff(org, "manager")
    viewer = await staff(org, "viewer")
    billing = await staff(org, "billing")
    acme = await make_client(org)
    theirs = await log_time(client, colleague, org, client_id=acme.id)
    url = org_url(org, f"/time-entries/{theirs['id']}")

    # Staff: own only.
    assert (await client.get(url, headers=person.headers)).status_code == 403
    assert (await client.patch(url, headers=person.headers, json={"minutes": 5})).status_code == 403
    assert (await client.delete(url, headers=person.headers)).status_code == 403
    await log_time(client, person, org, status=403, user_id=str(colleague.id))
    response = await client.get(
        org_url(org, "/time-entries"), headers=person.headers, params={"user_id": str(colleague.id)}
    )
    assert response.status_code == 403
    response = await client.get(
        org_url(org, "/time-entries"), headers=person.headers, params={"user_id": "all"}
    )
    assert response.status_code == 403
    # Viewer and billing read everyone's time but cannot log any.
    for reader in (viewer, billing):
        assert (await client.get(url, headers=reader.headers)).status_code == 200
        await log_time(client, reader, org, status=403)
        assert (
            await client.patch(url, headers=reader.headers, json={"minutes": 5})
        ).status_code == 403
    # Manager: manage_all.
    for_colleague = await log_time(client, manager, org, user_id=str(colleague.id), minutes=15)
    assert for_colleague["user_id"] == str(colleague.id)
    assert for_colleague["created_by"] == str(manager.id)
    assert for_colleague["cost_rate"] is not None  # managers hold rate.read
    assert (
        await client.patch(url, headers=manager.headers, json={"minutes": 5})
    ).status_code == 200
    assert (await client.delete(url, headers=manager.headers)).status_code == 200
    # Only active members can be given time.
    suspended = await staff(org, status="suspended")
    error = await log_time(client, owner, org, status=422, user_id=str(suspended.id))
    assert error["details"][0]["field"] == "user_id"
    await log_time(client, owner, org, status=422, user_id=str(uuid.uuid4()))
    response = await client.get(
        org_url(org, "/time-entries"), headers=person.headers, params={"user_id": "bogus"}
    )
    assert response.status_code == 422


async def test_tenant_isolation(client: AsyncClient) -> None:
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other & Co")
    entry = await log_time(client, other_owner, other)
    url = org_url(org, f"/time-entries/{entry['id']}")
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    assert (await client.patch(url, headers=owner.headers, json={"minutes": 5})).status_code == 404
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    listed = ok_data(
        await client.get(
            org_url(org, "/time-entries"), headers=owner.headers, params={"user_id": "all"}
        )
    )
    assert listed == []
    await log_time(client, owner, org, status=422, user_id=str(other_owner.id))
    response = await client.get(org_url(other, "/time-entries"), headers=owner.headers)
    assert response.status_code == 404


async def test_list_filters_and_pagination(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    beta = await make_client(org)
    job = await make_job(org, acme)
    e1 = await log_time(client, person, org, job_id=job.id, entry_date="2026-09-07")
    e2 = await log_time(client, person, org, client_id=beta.id, entry_date="2026-09-08")
    e3 = await log_time(client, person, org, entry_date="2026-09-15")
    e4 = await log_time(client, owner, org, client_id=acme.id, entry_date="2026-09-09")
    await set_entry(e2["id"], billing_status="billed")
    await set_sheet(e3["timesheet_id"], status="submitted")
    base = org_url(org, "/time-entries")

    async def ids(actor=person, **params) -> list[str]:
        return [
            e["id"] for e in ok_data(await client.get(base, headers=actor.headers, params=params))
        ]

    assert await ids() == [e3["id"], e2["id"], e1["id"]]
    assert await ids(user_id="me") == [e3["id"], e2["id"], e1["id"]]
    assert await ids(owner, user_id="all") == [e3["id"], e4["id"], e2["id"], e1["id"]]
    assert await ids(owner, user_id=str(person.id)) == [e3["id"], e2["id"], e1["id"]]
    assert await ids(date_from="2026-09-08", date_to="2026-09-14") == [e2["id"]]
    assert await ids(owner, user_id="all", client_id=str(acme.id)) == [e4["id"], e1["id"]]
    assert await ids(job_id=str(job.id)) == [e1["id"]]
    assert await ids(billable="false") == [e3["id"]]
    assert await ids(billable="true") == [e2["id"], e1["id"]]
    assert await ids(billing_status="billed") == [e2["id"]]
    assert await ids(billing_status=["billed", "unbilled"]) == [e3["id"], e2["id"], e1["id"]]
    assert await ids(timesheet_status="submitted") == [e3["id"]]
    assert await ids(timesheet_status="open") == [e2["id"], e1["id"]]
    seen, cursor = [], None
    while True:
        params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
        body = (await client.get(base, headers=person.headers, params=params)).json()
        seen += [e["id"] for e in body["data"]]
        cursor = body["meta"]["next_cursor"]
        if not cursor:
            break
    assert seen == [e3["id"], e2["id"], e1["id"]]
    assert (
        await client.get(base, headers=person.headers, params={"billing_status": "nope"})
    ).status_code == 422


async def test_week_grid(client: AsyncClient) -> None:
    owner, org = await practice(client)
    await set_practice(org, time_increment_minutes=1)
    person = await staff(org)
    manager = await staff(org, "manager")
    stranger = await staff(org)
    acme = await make_client(org, "Acme Ltd")
    job = await make_job(org, acme, "Accounts")
    acc = await activity_code_id(org, "ACC")
    admin = await activity_code_id(org, "ADM")
    await log_time(
        client,
        person,
        org,
        job_id=job.id,
        activity_code_id=acc,
        minutes=60,
        entry_date="2026-09-07",
    )
    await log_time(
        client,
        person,
        org,
        job_id=job.id,
        activity_code_id=acc,
        minutes=30,
        entry_date="2026-09-07",
    )
    await log_time(
        client,
        person,
        org,
        job_id=job.id,
        activity_code_id=acc,
        minutes=45,
        entry_date="2026-09-09",
    )
    await log_time(
        client, person, org, activity_code_id=admin, minutes=120, entry_date="2026-09-08"
    )
    await log_time(client, person, org, minutes=10, entry_date="2026-09-14")  # next week

    url = org_url(org, "/time-entries/week")
    grid = ok_data(
        await client.get(url, headers=person.headers, params={"week_start": "2026-09-10"})
    )
    assert grid["week_start"] == "2026-09-07" and grid["week_end"] == "2026-09-13"
    assert len(grid["days"]) == 7
    assert grid["timesheet_status"] == "open" and grid["timesheet_id"]
    assert len(grid["rows"]) == 2
    first, second = grid["rows"]
    assert first["client_name"] == "Acme Ltd" and first["activity_code"] == "ACC"
    assert first["minutes"] == [90, 0, 45, 0, 0, 0, 0] and first["total_minutes"] == 135
    assert len(first["entry_ids"]) == 3
    assert second["client_id"] is None and second["minutes"] == [0, 120, 0, 0, 0, 0, 0]
    assert grid["day_totals"] == [90, 120, 45, 0, 0, 0, 0]
    assert grid["total_minutes"] == 255
    assert grid["chargeable_minutes"] == 135 and grid["non_chargeable_minutes"] == 120

    empty = ok_data(
        await client.get(url, headers=person.headers, params={"week_start": "2026-01-05"})
    )
    assert (
        empty["rows"] == []
        and empty["timesheet_id"] is None
        and empty["timesheet_status"] == "open"
    )
    assert empty["day_totals"] == [0] * 7
    this_week = ok_data(await client.get(url, headers=person.headers))
    assert date.fromisoformat(this_week["week_start"]).isoweekday() == 1

    # Others: time.read_all or their manager.
    params = {"week_start": "2026-09-07", "user_id": str(person.id)}
    assert (await client.get(url, headers=stranger.headers, params=params)).status_code == 403
    assert (await client.get(url, headers=owner.headers, params=params)).status_code == 200
    await set_manager(org, person, stranger)
    assert (await client.get(url, headers=stranger.headers, params=params)).status_code == 200
    assert (await client.get(url, headers=manager.headers, params=params)).status_code == 200


async def test_entries_stay_editable_after_week_start_change(client: AsyncClient) -> None:
    """The entry's own sheet is used, not one re-derived from the current week_start."""
    _, org = await practice(client)
    await set_practice(org, time_increment_minutes=1)
    person = await staff(org)
    entry = await log_time(client, person, org, entry_date="2026-09-08", minutes=30)  # Tue
    other = await log_time(client, person, org, entry_date="2026-09-09", minutes=15)  # Wed
    await set_practice(org, week_start=7)  # Sunday from now on
    url = org_url(org, f"/time-entries/{entry['id']}")
    edited = ok_data(await client.patch(url, headers=person.headers, json={"minutes": 45}))
    assert edited["minutes"] == 45 and edited["timesheet_id"] == entry["timesheet_id"]
    # A new date inside its sheet's week keeps the sheet.
    moved = ok_data(
        await client.patch(url, headers=person.headers, json={"entry_date": "2026-09-13"})
    )
    assert moved["timesheet_id"] == entry["timesheet_id"]
    # Outside it, the entry moves to the sheet of the new week (Sunday-based).
    moved = ok_data(
        await client.patch(url, headers=person.headers, json={"entry_date": "2026-09-15"})
    )
    assert moved["timesheet_id"] != entry["timesheet_id"]
    sheet = ok_data(
        await client.get(
            org_url(org, f"/timesheets/{moved['timesheet_id']}"), headers=person.headers
        )
    )
    assert sheet["week_start"] == "2026-09-13"
    # The old sheet's lock still applies to what is left on it.
    await set_sheet(entry["timesheet_id"], status="submitted")
    other_url = org_url(org, f"/time-entries/{other['id']}")
    assert err(await client.delete(other_url, headers=person.headers), 409)["code"] == (
        "TIME_LOCKED"
    )
    await set_sheet(entry["timesheet_id"], status="open")
    assert (await client.delete(other_url, headers=person.headers)).status_code == 200


async def test_entry_dates_are_bounded(client: AsyncClient) -> None:
    _, org = await practice(client)
    await set_practice(org, week_start=7)
    person = await staff(org)
    for bad in ("0001-01-01", "9999-12-31"):
        await log_time(client, person, org, status=422, entry_date=bad)
        response = await client.get(
            org_url(org, "/time-entries/week"), headers=person.headers, params={"week_start": bad}
        )
        assert response.status_code == 422
    entry = await log_time(client, person, org)
    response = await client.patch(
        org_url(org, f"/time-entries/{entry['id']}"),
        headers=person.headers,
        json={"entry_date": "9999-12-31"},
    )
    assert response.status_code == 422


async def test_time_waits_for_a_concurrent_job_close(client: AsyncClient) -> None:
    """Logging time takes a FOR KEY SHARE lock on the job: a job being completed at the
    same moment is seen as closed, never written to."""
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    job = await make_job(org, acme)
    async with get_sessionmaker()() as session:
        # What JobService.change_status does: lock the job, change it, commit.
        await session.execute(select(Job).where(Job.id == job.id).with_for_update())
        task = asyncio.create_task(log_time(client, person, org, status=409, job_id=job.id))
        await asyncio.sleep(0.5)
        assert not task.done()
        await session.execute(update(Job).where(Job.id == job.id).values(status="completed"))
        await session.commit()
    error = await task
    assert error["code"] == "JOB_CLOSED"
    listed = ok_data(await client.get(org_url(org, "/time-entries"), headers=person.headers))
    assert listed == []


async def test_bulk_create(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    await make_staff_rate(org, person, 12_000)
    acme = await make_client(org)
    job = await make_job(org, acme)
    url = org_url(org, "/time-entries/bulk")
    body = {
        "entries": [
            {"entry_date": f"2026-09-{d:02d}", "minutes": 60, "job_id": str(job.id)}
            for d in range(7, 12)
        ]
        + [{"entry_date": "2026-09-14", "minutes": 30}]
    }
    created = ok_data(await client.post(url, headers=person.headers, json=body), 201)
    assert len(created) == 6
    assert all(e["charge_value"] == 12_000 for e in created[:5])
    assert len({e["timesheet_id"] for e in created}) == 2
    assert len(await audit_entries(org["id"], "time_entry.created")) == 6

    # Atomic: one bad entry rolls back the rest, and the error names it.
    closed = await make_job(org, acme, status="completed")
    bad = {
        "entries": [
            {"entry_date": "2026-09-21", "minutes": 60},
            {"entry_date": "2026-09-21", "minutes": 60, "job_id": str(uuid.uuid4())},
        ]
    }
    error = err(await client.post(url, headers=person.headers, json=bad), 404)
    assert error["details"]["field"] == "entries.1.job_id"
    bad = {
        "entries": [
            {"entry_date": "2026-09-21", "minutes": 60},
            {"entry_date": "2026-09-21", "minutes": 60, "job_id": str(closed.id)},
        ]
    }
    assert (
        err(await client.post(url, headers=person.headers, json=bad), 409)["code"] == "JOB_CLOSED"
    )
    # The cap counts entries within the batch.
    bad = {
        "entries": [
            {"entry_date": "2026-09-21", "minutes": 800},
            {"entry_date": "2026-09-21", "minutes": 700},
        ]
    }
    error = err(await client.post(url, headers=person.headers, json=bad), 422)
    assert (
        error["code"] == "DAY_LIMIT_EXCEEDED"
        and error["details"][0]["field"] == "entries.1.minutes"
    )
    listed = ok_data(
        await client.get(
            org_url(org, "/time-entries"),
            headers=person.headers,
            params={"date_from": "2026-09-21"},
        )
    )
    assert listed == []
    # Limits and validation.
    assert (await client.post(url, headers=person.headers, json={"entries": []})).status_code == 422
    many = {"entries": [{"entry_date": "2026-09-28", "minutes": 1}] * 101}
    assert (await client.post(url, headers=person.headers, json=many)).status_code == 422
    other = await staff(org)
    mixed = {"entries": [{"entry_date": "2026-09-28", "minutes": 1, "user_id": str(other.id)}]}
    assert (await client.post(url, headers=person.headers, json=mixed)).status_code == 403

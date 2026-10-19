"""The running timer: one per person, pause/resume, stop writes an entry, discard."""

import asyncio
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import delete, select, update

from app.db.models import Job, Timer
from app.db.session import get_sessionmaker
from tests.domain_factories import make_client, make_job, make_staff_rate, practice, staff
from tests.factories import audit_entries
from tests.time_helpers import age_timer, err, ok_data, org_url, set_practice


async def test_start_pause_resume_stop(client: AsyncClient) -> None:
    _, org = await practice(client)
    await set_practice(org, time_increment_minutes=1)
    person = await staff(org)
    await make_staff_rate(org, person, 12_000)
    acme = await make_client(org)
    job = await make_job(org, acme, "Accounts")
    assert ok_data(await client.get(org_url(org, "/timer"), headers=person.headers)) is None

    timer = ok_data(
        await client.post(
            org_url(org, "/timer/start"),
            headers=person.headers,
            json={"job_id": str(job.id), "description": "Bank reconciliation"},
        ),
        201,
    )
    assert timer["is_running"] is True and timer["client_id"] == str(acme.id)
    assert timer["is_billable"] is True and timer["elapsed_seconds"] < 5

    await age_timer(org, person, 590)
    paused = ok_data(await client.post(org_url(org, "/timer/pause"), headers=person.headers))
    assert paused["is_running"] is False and paused["started_at"] is None
    assert 590 <= paused["accumulated_seconds"] < 595
    error = err(await client.post(org_url(org, "/timer/pause"), headers=person.headers), 409)
    assert error["code"] == "TIMER_NOT_RUNNING"
    still = ok_data(await client.get(org_url(org, "/timer"), headers=person.headers))
    assert still["elapsed_seconds"] == paused["accumulated_seconds"]

    resumed = ok_data(await client.post(org_url(org, "/timer/resume"), headers=person.headers))
    assert resumed["is_running"] is True
    error = err(await client.post(org_url(org, "/timer/resume"), headers=person.headers), 409)
    assert error["code"] == "TIMER_RUNNING"

    entry = ok_data(await client.post(org_url(org, "/timer/stop"), headers=person.headers), 201)
    assert entry["minutes"] == 10  # 590-595 s rounds up to 10 minutes
    assert entry["job_id"] == str(job.id) and entry["description"] == "Bank reconciliation"
    assert entry["entry_date"] == timer["started_on"]
    assert entry["charge_value"] == 2_000
    assert ok_data(await client.get(org_url(org, "/timer"), headers=person.headers)) is None
    log = await audit_entries(org["id"], "time_entry.created")
    assert log[0]["metadata"]["source"] == "timer"
    for action in ("pause", "resume", "stop"):
        response = await client.post(org_url(org, f"/timer/{action}"), headers=person.headers)
        assert err(response, 404)["code"] == "TIMER_NOT_FOUND"


async def test_stop_rounds_to_increment_with_minimum(client: AsyncClient) -> None:
    _, org = await practice(client)
    await set_practice(org, time_increment_minutes=15)
    person = await staff(org)
    await client.post(org_url(org, "/timer/start"), headers=person.headers, json={})
    entry = ok_data(
        await client.post(
            org_url(org, "/timer/stop"), headers=person.headers, json={"description": "Admin"}
        ),
        201,
    )
    assert entry["minutes"] == 15 and entry["description"] == "Admin"
    assert entry["is_billable"] is False  # no client

    await client.post(org_url(org, "/timer/start"), headers=person.headers, json={})
    await age_timer(org, person, 16 * 60 + 1)
    entry = ok_data(await client.post(org_url(org, "/timer/stop"), headers=person.headers), 201)
    assert entry["minutes"] == 30


async def test_one_timer_per_person_and_replace(client: AsyncClient) -> None:
    _, org = await practice(client)
    await set_practice(org, time_increment_minutes=1)
    person = await staff(org)
    acme = await make_client(org)
    other = await staff(org)
    url = org_url(org, "/timer/start")
    first = ok_data(
        await client.post(url, headers=person.headers, json={"client_id": str(acme.id)}), 201
    )
    error = err(await client.post(url, headers=person.headers, json={}), 409)
    assert error["code"] == "TIMER_RUNNING"
    # Somebody else has their own.
    ok_data(await client.post(url, headers=other.headers, json={}), 201)

    await age_timer(org, person, 125)
    second = ok_data(
        await client.post(
            url, headers=person.headers, json={"replace": True, "description": "Next"}
        ),
        201,
    )
    assert second["id"] != first["id"] and second["description"] == "Next"
    entries = ok_data(await client.get(org_url(org, "/time-entries"), headers=person.headers))
    assert len(entries) == 1 and entries[0]["minutes"] == 3
    assert entries[0]["client_id"] == str(acme.id)


async def test_parallel_starts_leave_one_timer(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    responses = await asyncio.gather(
        *(
            client.post(org_url(org, "/timer/start"), headers=person.headers, json={})
            for _ in range(5)
        )
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 409, 409, 409, 409]
    assert all(
        r.json()["error"]["code"] == "TIMER_RUNNING" for r in responses if r.status_code == 409
    )


async def test_parallel_stops_write_one_entry(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    await client.post(org_url(org, "/timer/start"), headers=person.headers, json={})
    responses = await asyncio.gather(
        *(client.post(org_url(org, "/timer/stop"), headers=person.headers) for _ in range(3))
    )
    assert sorted(r.status_code for r in responses) == [201, 404, 404]
    entries = ok_data(await client.get(org_url(org, "/time-entries"), headers=person.headers))
    assert len(entries) == 1


async def test_discard(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    await client.post(org_url(org, "/timer/start"), headers=person.headers, json={})
    assert (await client.delete(org_url(org, "/timer"), headers=person.headers)).status_code == 200
    assert ok_data(await client.get(org_url(org, "/timer"), headers=person.headers)) is None
    assert ok_data(await client.get(org_url(org, "/time-entries"), headers=person.headers)) == []
    assert (await client.delete(org_url(org, "/timer"), headers=person.headers)).status_code == 404


async def test_start_rules(client: AsyncClient) -> None:
    _, org = await practice(client)
    _, other = await practice(client, "Other & Co")
    person = await staff(org)
    archived = await make_client(org, status="archived")
    acme = await make_client(org)
    closed = await make_job(org, acme, status="cancelled")
    internal = await make_job(org, acme, billing_basis="non_billable")
    theirs = await make_client(other)
    url = org_url(org, "/timer/start")
    assert (
        err(
            await client.post(url, headers=person.headers, json={"client_id": str(archived.id)}),
            409,
        )["code"]
        == "CLIENT_INACTIVE"
    )
    assert (
        err(await client.post(url, headers=person.headers, json={"job_id": str(closed.id)}), 409)[
            "code"
        ]
        == "JOB_CLOSED"
    )
    assert (
        await client.post(url, headers=person.headers, json={"client_id": str(theirs.id)})
    ).status_code == 404
    assert (
        await client.post(url, headers=person.headers, json={"is_billable": True})
    ).status_code == 422
    assert (await client.post(url, headers=person.headers, json={"bogus": 1})).status_code == 422
    timer = ok_data(
        await client.post(url, headers=person.headers, json={"job_id": str(internal.id)}), 201
    )
    assert timer["is_billable"] is False


async def test_stop_respects_time_rules(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    await client.post(org_url(org, "/timer/start"), headers=person.headers, json={})
    timer = ok_data(await client.get(org_url(org, "/timer"), headers=person.headers))
    # Lock this week's sheet: the entry cannot be written and the timer is kept.
    from tests.time_helpers import log_time, set_sheet

    entry = await log_time(client, person, org, entry_date=timer["started_on"])
    await set_sheet(entry["timesheet_id"], status="submitted")
    error = err(await client.post(org_url(org, "/timer/stop"), headers=person.headers), 409)
    assert error["code"] == "TIME_LOCKED"
    assert ok_data(await client.get(org_url(org, "/timer"), headers=person.headers)) is not None


async def test_stop_racing_job_delete_does_not_deadlock(client: AsyncClient) -> None:
    """Stop locks the job before the timer (the order job deletion uses): it waits for the
    delete, then finds its timer gone."""
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    job = await make_job(org, acme)
    await client.post(
        org_url(org, "/timer/start"), headers=person.headers, json={"job_id": str(job.id)}
    )
    async with get_sessionmaker()() as session:
        await session.execute(select(Job).where(Job.id == job.id).with_for_update())
        task = asyncio.create_task(client.post(org_url(org, "/timer/stop"), headers=person.headers))
        await asyncio.sleep(0.5)
        assert not task.done()
        # Would deadlock if the stop already held the timer row.
        await session.execute(delete(Timer).where(Timer.job_id == job.id))
        await session.execute(
            update(Job).where(Job.id == job.id).values(deleted_at=datetime.now(UTC))
        )
        await session.commit()
    response = await task
    assert err(response, 404)["code"] == "TIMER_NOT_FOUND"
    assert ok_data(await client.get(org_url(org, "/time-entries"), headers=person.headers)) == []


async def test_start_racing_job_delete(client: AsyncClient) -> None:
    """A timer is never left on a job deleted at the same moment."""
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    job = await make_job(org, acme)
    async with get_sessionmaker()() as session:
        await session.execute(select(Job).where(Job.id == job.id).with_for_update())
        task = asyncio.create_task(
            client.post(
                org_url(org, "/timer/start"),
                headers=person.headers,
                json={"job_id": str(job.id)},
            )
        )
        await asyncio.sleep(0.5)
        assert not task.done()
        await session.execute(delete(Timer).where(Timer.job_id == job.id))
        await session.execute(
            update(Job).where(Job.id == job.id).values(deleted_at=datetime.now(UTC))
        )
        await session.commit()
    response = await task
    assert response.status_code == 404, response.text
    assert ok_data(await client.get(org_url(org, "/timer"), headers=person.headers)) is None


async def test_permissions_and_isolation(client: AsyncClient) -> None:
    _, org = await practice(client)
    viewer = await staff(org, "viewer")
    for method, path in (
        ("get", "/timer"),
        ("post", "/timer/start"),
        ("post", "/timer/pause"),
        ("post", "/timer/resume"),
        ("post", "/timer/stop"),
        ("delete", "/timer"),
    ):
        kwargs = {"json": {}} if path == "/timer/start" else {}
        response = await client.request(
            method, org_url(org, path), headers=viewer.headers, **kwargs
        )
        assert response.status_code == 403, path
    other_owner, other = await practice(client, "Other & Co")
    await client.post(org_url(other, "/timer/start"), headers=other_owner.headers, json={})
    response = await client.get(org_url(org, "/timer"), headers=other_owner.headers)
    assert response.status_code == 404

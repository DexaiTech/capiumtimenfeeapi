"""Job statuses: the transition table, the completion and closing times, and a closed job taking
no new time or task changes."""

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from tests.domain_factories import make_client, practice, staff
from tests.factories import audit_entries
from tests.job_helpers import add_task, create_job, job_url, log_on_job, set_job_row, task_url
from tests.time_helpers import err, ok_data, org_url

TRANSITIONS = {
    "planned": {"in_progress", "on_hold", "closed", "cancelled"},
    "in_progress": {"on_hold", "completed", "closed", "cancelled"},
    "on_hold": {"in_progress", "closed", "cancelled"},
    "completed": {"closed", "in_progress"},
    "closed": {"in_progress"},
    "cancelled": set(),
}
STATUSES = tuple(TRANSITIONS)


async def _move(client: AsyncClient, actor, org, job_id: str, target: str, status: int = 200):
    response = await client.post(
        job_url(org, job_id, "/status"), headers=actor.headers, json={"status": target}
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status == 200 else response.json()["error"]


async def _job_in(client: AsyncClient, owner, org, customer, status: str) -> dict:
    """A job saved in the given status (the times that go with it are set as the API sets them)."""
    job = await create_job(client, owner, org, customer, f"In {status}")
    now = datetime.now(UTC)
    values: dict = {"status": status}
    if status in ("completed", "closed"):
        values["completed_at"] = now
    if status == "closed":
        values["closed_at"] = now
    await set_job_row(job["id"], **values)
    return job


@pytest.mark.parametrize("old", STATUSES)
async def test_the_transition_table(client: AsyncClient, old: str) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    for target in STATUSES:
        job = await _job_in(client, owner, org, customer, old)
        if target in TRANSITIONS[old]:
            moved = await _move(client, owner, org, job["id"], target)
            assert moved["status"] == target, (old, target)
        else:
            error = await _move(client, owner, org, job["id"], target, 409)
            assert error["code"] == "INVALID_STATUS_TRANSITION", (old, target)
            assert error["details"] == {
                "from": old,
                "to": target,
                "allowed": sorted(TRANSITIONS[old]),
            }
            read = ok_data(await client.get(job_url(org, job["id"]), headers=owner.headers))
            assert read["status"] == old


async def test_an_unknown_status_is_refused(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await create_job(client, owner, org, await make_client(org))
    for bogus in ("open", "not_started", "archived", ""):
        response = await client.post(
            job_url(org, job["id"], "/status"), headers=owner.headers, json={"status": bogus}
        )
        assert response.status_code == 422, bogus


async def test_completion_and_closing_times(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await create_job(client, owner, org, await make_client(org))
    started = await _move(client, owner, org, job["id"], "in_progress")
    assert started["completed_at"] is None and started["closed_at"] is None
    done = await _move(client, owner, org, job["id"], "completed")
    assert done["completed_at"] is not None and done["closed_at"] is None
    closed = await _move(client, owner, org, job["id"], "closed")
    # Closing a finished job keeps when it was completed and says when it was closed.
    assert closed["completed_at"] == done["completed_at"] and closed["closed_at"] is not None
    reopened = await _move(client, owner, org, job["id"], "in_progress")
    assert reopened["completed_at"] is None and reopened["closed_at"] is None
    # A job can be closed without ever being completed.
    shut = await _move(client, owner, org, job["id"], "closed")
    assert shut["completed_at"] is None and shut["closed_at"] is not None
    # A completed job reopens straight to in progress.
    again = await _move(client, owner, org, job["id"], "in_progress")
    assert again["closed_at"] is None
    await _move(client, owner, org, job["id"], "completed")
    assert (await _move(client, owner, org, job["id"], "in_progress"))["completed_at"] is None
    log = await audit_entries(org["id"], "job.status_changed")
    assert [(e["metadata"]["old"], e["metadata"]["new"]) for e in log] == [
        ("planned", "in_progress"),
        ("in_progress", "completed"),
        ("completed", "closed"),
        ("closed", "in_progress"),
        ("in_progress", "closed"),
        ("closed", "in_progress"),
        ("in_progress", "completed"),
        ("completed", "in_progress"),
    ]


async def test_changing_status_needs_job_update(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await create_job(client, owner, org, await make_client(org))
    for role in ("staff", "viewer", "billing"):
        person = await staff(org, role)
        await _move(client, person, org, job["id"], "in_progress", 403)
    manager = await staff(org, "manager")
    assert (await _move(client, manager, org, job["id"], "in_progress"))["status"] == "in_progress"
    other_owner, _ = await practice(client, "Other & Co")
    await _move(client, other_owner, org, job["id"], "closed", 404)


@pytest.mark.parametrize("status", ["completed", "closed", "cancelled"])
async def test_a_finished_job_takes_no_new_time(client: AsyncClient, status: str) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    job = await _job_in(client, owner, org, customer, status)
    error = await log_on_job(client, owner, org, job, status=409)
    assert error["code"] == "JOB_CLOSED"
    assert error["details"] == {"job_id": job["id"], "status": status}
    assert "closed" in error["message"]
    started = await client.post(
        org_url(org, "/timer/start"), headers=owner.headers, json={"job_id": job["id"]}
    )
    assert started.status_code == 409 and started.json()["error"]["code"] == "JOB_CLOSED"


async def test_closed_job_time_is_refused_everywhere(client: AsyncClient) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    open_job = await create_job(client, owner, org, customer, "Open")
    shut = await _job_in(client, owner, org, customer, "closed")
    entry = await log_on_job(client, owner, org, open_job, 60)

    # Bulk entry: one closed job rolls the whole batch back.
    def item(job: dict) -> dict:
        return {
            "entry_date": "2026-09-08",
            "minutes": 30,
            "job_id": job["id"],
            "client_id": str(customer.id),
        }

    response = await client.post(
        org_url(org, "/time-entries/bulk"),
        headers=owner.headers,
        json={"entries": [item(open_job), item(shut)]},
    )
    assert err(response, 409)["code"] == "JOB_CLOSED"
    listed = ok_data(await client.get(org_url(org, "/time-entries"), headers=owner.headers))
    assert [e["id"] for e in listed] == [entry["id"]]

    # Moving time onto a closed job.
    url = org_url(org, f"/time-entries/{entry['id']}")
    moved = await client.patch(url, headers=owner.headers, json={"job_id": shut["id"]})
    assert err(moved, 409)["code"] == "JOB_CLOSED"

    # Time that was there before stays and can still be described.
    await log_on_job(client, owner, org, open_job, 30, entry_date="2026-09-09")
    await set_job_row(open_job["id"], status="closed", closed_at=datetime.now(UTC))
    described = await client.patch(url, headers=owner.headers, json={"description": "Notes"})
    assert ok_data(described)["description"] == "Notes"
    assert (await client.patch(url, headers=owner.headers, json={"minutes": 90})).status_code == 409
    assert (await client.delete(url, headers=owner.headers)).status_code == 200


async def test_a_running_timer_survives_a_closed_job(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await create_job(client, owner, org, await make_client(org))
    started = await client.post(
        org_url(org, "/timer/start"), headers=owner.headers, json={"job_id": job["id"]}
    )
    assert started.status_code == 201, started.text
    await _move(client, owner, org, job["id"], "closed")
    stopped = await client.post(org_url(org, "/timer/stop"), headers=owner.headers)
    assert err(stopped, 409)["code"] == "JOB_CLOSED"
    assert (
        ok_data(await client.get(org_url(org, "/timer"), headers=owner.headers))["job_id"]
        == job["id"]
    )
    # Reopened, the same timer stops into an entry.
    await _move(client, owner, org, job["id"], "in_progress")
    stopped = await client.post(org_url(org, "/timer/stop"), headers=owner.headers)
    assert stopped.status_code == 201, stopped.text


async def test_on_hold_and_reopened_jobs_take_time(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await create_job(client, owner, org, await make_client(org))
    await _move(client, owner, org, job["id"], "on_hold")
    await log_on_job(client, owner, org, job, 30)
    await _move(client, owner, org, job["id"], "closed")
    await log_on_job(client, owner, org, job, 30, status=409)
    await _move(client, owner, org, job["id"], "in_progress")
    await log_on_job(client, owner, org, job, 30, entry_date="2026-09-08")


async def test_closing_a_job_locks_its_tasks_and_reopening_unlocks_them(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    job = await create_job(client, owner, org, await make_client(org))
    task = await add_task(client, owner, org, job, "Draft")
    await _move(client, owner, org, job["id"], "closed")
    error = await add_task(client, owner, org, job, "More", expect=409)
    assert error["code"] == "JOB_CLOSED"
    changed = await client.post(
        task_url(org, job["id"], task["id"], "/status"),
        headers=owner.headers,
        json={"status": "completed"},
    )
    assert err(changed, 409)["code"] == "JOB_CLOSED"
    await _move(client, owner, org, job["id"], "in_progress")
    await add_task(client, owner, org, job, "More")

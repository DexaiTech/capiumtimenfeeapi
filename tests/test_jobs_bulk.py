"""POST /jobs/bulk: set status, manager and due date, and delete, on up to 200 jobs, all or
nothing, with the rules of the single operations."""

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient

from tests.domain_factories import make_client, practice, staff
from tests.factories import audit_entries
from tests.job_helpers import create_job, job_row, log_on_job, role_without, set_job_row
from tests.time_helpers import ok_data, org_url


def _url(org: dict) -> str:
    return org_url(org, "/jobs/bulk")


async def _bulk(client: AsyncClient, actor, org, ids, action, status=200, **extra):
    response = await client.post(
        _url(org),
        headers=actor.headers,
        json={"job_ids": [str(i) for i in ids], "action": action, **extra},
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status == 200 else response.json()["error"]


async def _three(client: AsyncClient):
    owner, org = await practice(client)
    customer = await make_client(org)
    jobs = [await create_job(client, owner, org, customer, f"Job {n}") for n in range(3)]
    return owner, org, customer, jobs


async def test_set_status_moves_every_job_and_audits_each(client: AsyncClient) -> None:
    owner, org, _, jobs = await _three(client)
    ids = [j["id"] for j in jobs]
    result = await _bulk(client, owner, org, ids, "set_status", value="in_progress")
    assert result == {"action": "set_status", "affected": 3, "changed": 3}
    assert {(await job_row(i)).status for i in ids} == {"in_progress"}
    rows = await audit_entries(org["id"], "job.status_changed")
    assert len(rows) == 3
    assert all(
        r["metadata"]["bulk"] is True and r["metadata"]["new"] == "in_progress" for r in rows
    )
    # The same again changes nothing: the jobs are already there.
    again = await _bulk(client, owner, org, ids, "set_status", value="in_progress")
    assert again["affected"] == 3 and again["changed"] == 0
    assert len(await audit_entries(org["id"], "job.status_changed")) == 3


async def test_a_refused_transition_changes_nothing(client: AsyncClient) -> None:
    owner, org, _, jobs = await _three(client)
    await set_job_row(jobs[2]["id"], status="cancelled")
    ids = [j["id"] for j in jobs]
    error = await _bulk(client, owner, org, ids, "set_status", 409, value="in_progress")
    assert error["code"] == "INVALID_STATUS_TRANSITION"
    assert [d["job_id"] for d in error["details"]] == [jobs[2]["id"]]
    assert error["details"][0]["number"] == jobs[2]["number"]
    assert (await job_row(jobs[0]["id"])).status == "planned"


async def test_wrong_values_and_unknown_ids(client: AsyncClient) -> None:
    owner, org, _, jobs = await _three(client)
    ids = [jobs[0]["id"]]
    await _bulk(client, owner, org, ids, "set_status", 422, value="nope")
    await _bulk(client, owner, org, ids, "set_status", 422)
    await _bulk(client, owner, org, ids, "set_manager", 422, value="not-a-uuid")
    await _bulk(client, owner, org, ids, "set_due_date", 422, value="31/12/2026")
    await _bulk(client, owner, org, ids, "delete", 422, value="x")
    await _bulk(client, owner, org, [], "delete", 422)
    await _bulk(client, owner, org, [uuid.uuid4()] * 201, "delete", 422)
    ghost = str(uuid.uuid4())
    error = await _bulk(client, owner, org, [*ids, ghost], "set_status", 404, value="on_hold")
    assert error["details"] == {"job_ids": [ghost]}
    assert (await job_row(ids[0])).status == "planned"


async def test_another_practices_jobs_are_not_found(client: AsyncClient) -> None:
    owner, org, _, jobs = await _three(client)
    _, _, _, others = await _three(client)
    error = await _bulk(
        client, owner, org, [jobs[0]["id"], others[0]["id"]], "set_status", 404, value="on_hold"
    )
    assert error["details"] == {"job_ids": [others[0]["id"]]}
    assert (await job_row(others[0]["id"])).status == "planned"


async def test_set_and_clear_the_manager(client: AsyncClient) -> None:
    owner, org, _, jobs = await _three(client)
    manager = await staff(org, "manager")
    ids = [j["id"] for j in jobs]
    result = await _bulk(client, owner, org, ids, "set_manager", value=str(manager.id))
    assert result["changed"] == 3
    assert {(await job_row(i)).manager_user_id for i in ids} == {manager.id}
    entry = (await audit_entries(org["id"], "job.updated"))[0]
    assert entry["metadata"]["bulk"] is True
    assert entry["metadata"]["changes"]["manager_user_id"] == [None, str(manager.id)]
    cleared = await _bulk(client, owner, org, ids[:2], "set_manager")
    assert cleared == {"action": "set_manager", "affected": 2, "changed": 2}
    assert (await job_row(ids[0])).manager_user_id is None
    assert (await job_row(ids[2])).manager_user_id == manager.id


async def test_the_manager_must_be_an_active_member(client: AsyncClient) -> None:
    owner, org, _, jobs = await _three(client)
    outsider = str(uuid.uuid4())
    await _bulk(client, owner, org, [jobs[0]["id"]], "set_manager", 422, value=outsider)
    assert (await job_row(jobs[0]["id"])).manager_user_id is None


async def test_set_and_clear_the_due_date(client: AsyncClient) -> None:
    owner, org, _, jobs = await _three(client)
    ids = [j["id"] for j in jobs]
    result = await _bulk(client, owner, org, ids, "set_due_date", value="2027-01-31")
    assert result["changed"] == 3
    assert str((await job_row(ids[0])).due_date) == "2027-01-31"
    again = await _bulk(client, owner, org, ids, "set_due_date", value="2027-01-31")
    assert again["changed"] == 0
    await _bulk(client, owner, org, ids, "set_due_date")
    assert (await job_row(ids[1])).due_date is None


async def test_a_due_date_before_the_start_date_refuses_all(client: AsyncClient) -> None:
    owner, org, customer, _ = await _three(client)
    early = await create_job(client, owner, org, customer, "Later", start_date="2027-03-01")
    free = await create_job(client, owner, org, customer, "Free")
    error = await _bulk(
        client, owner, org, [early["id"], free["id"]], "set_due_date", 409, value="2027-02-01"
    )
    assert error["code"] == "JOB_DUE_DATE_INVALID"
    assert [d["job_id"] for d in error["details"]] == [early["id"]]
    assert (await job_row(free["id"])).due_date is None


async def test_a_job_linked_to_practice_management_keeps_its_manager_and_dates(
    client: AsyncClient,
) -> None:
    owner, org, _, jobs = await _three(client)
    await set_job_row(jobs[0]["id"], pm_job_id="PM-1", pm_linked_at=datetime.now(UTC))
    ids = [j["id"] for j in jobs]
    error = await _bulk(client, owner, org, ids, "set_due_date", 409, value="2027-01-31")
    assert error["code"] == "JOB_FIELD_MANAGED_BY_PM"
    assert [d["job_id"] for d in error["details"]] == [jobs[0]["id"]]
    assert (await job_row(jobs[1]["id"])).due_date is None


async def test_delete_removes_unused_jobs(client: AsyncClient) -> None:
    owner, org, _, jobs = await _three(client)
    ids = [j["id"] for j in jobs]
    result = await _bulk(client, owner, org, ids, "delete")
    assert result == {"action": "delete", "affected": 3, "changed": 3}
    assert (await job_row(ids[0])).deleted_at is not None
    rows = await audit_entries(org["id"], "job.deleted")
    assert len(rows) == 3 and all(r["metadata"]["bulk"] is True for r in rows)
    response = await client.get(org_url(org, "/jobs"), headers=owner.headers)
    assert ok_data(response) == []


async def test_delete_refuses_all_when_a_job_has_time(client: AsyncClient) -> None:
    owner, org, _, jobs = await _three(client)
    await log_on_job(client, owner, org, jobs[1])
    ids = [j["id"] for j in jobs]
    error = await _bulk(client, owner, org, ids, "delete", 409)
    assert error["code"] == "JOB_IN_USE"
    assert [d["job_id"] for d in error["details"]] == [jobs[1]["id"]]
    assert [(await job_row(i)).deleted_at for i in ids] == [None] * 3
    assert await audit_entries(org["id"], "job.deleted") == []


async def test_permissions_are_those_of_the_single_operations(client: AsyncClient) -> None:
    _owner, org, _, jobs = await _three(client)
    ids = [jobs[0]["id"]]
    manager = await staff(org, "manager")  # updates jobs, cannot delete them
    plain = await staff(org, "staff")  # cannot update jobs
    nobody = await role_without(org, "job.read")
    await _bulk(client, manager, org, ids, "set_status", value="on_hold")
    await _bulk(client, manager, org, ids, "delete", 403)
    await _bulk(client, plain, org, ids, "set_status", 403, value="in_progress")
    await _bulk(client, nobody, org, ids, "set_status", 403, value="in_progress")
    assert (await job_row(ids[0])).status == "on_hold"

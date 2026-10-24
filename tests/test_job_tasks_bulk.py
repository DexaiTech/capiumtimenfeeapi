"""Tasks in bulk: add many to a job, and change or delete many (of any jobs), all or nothing, with
the rules of the single endpoints."""

import uuid

from httpx import AsyncClient

from tests.domain_factories import make_client, practice, staff
from tests.factories import audit_entries, error
from tests.job_helpers import add_task, create_job, log_on_job, role_without, set_job_row, task_url
from tests.time_helpers import ok_data, org_url


async def _job(client: AsyncClient, owner, org, name="Accounts", **body):
    return await create_job(client, owner, org, await make_client(org), name, **body)


async def _add_many(client: AsyncClient, actor, org, job, tasks, status=201):
    response = await client.post(
        task_url(org, job["id"], suffix="/bulk")
        if False
        else org_url(org, f"/jobs/{job['id']}/tasks/bulk"),
        headers=actor.headers,
        json={"tasks": tasks},
    )
    assert response.status_code == status, response.text
    return response.json()


async def _bulk(client: AsyncClient, actor, org, ids, action, status=200, **extra):
    response = await client.post(
        org_url(org, "/tasks/bulk"),
        headers=actor.headers,
        json={"task_ids": [str(i) for i in ids], "action": action, **extra},
    )
    assert response.status_code == status, response.text
    body = response.json()
    return body["data"] if status == 200 else body["error"]


async def _titles(client: AsyncClient, owner, org, job) -> list[str]:
    listed = ok_data(await client.get(task_url(org, job["id"]), headers=owner.headers))
    return [t["title"] for t in listed]


# --- Add many -----------------------------------------------------------------------------


async def test_add_many_tasks_in_the_order_sent(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    ada = await staff(org, full_name="Ada Lovelace")
    body = await _add_many(
        client,
        owner,
        org,
        job,
        [
            {"title": "Prepare", "budget_minutes": 600, "assignee_user_ids": [str(ada.id)]},
            {"title": "Review", "status": "on_hold"},
            {"title": "File", "due_date": None},
        ],
    )
    assert body["message"] == "3 task(s) added"
    created = body["data"]
    assert [t["title"] for t in created] == ["Prepare", "Review", "File"]
    assert [t["sort_order"] for t in created] == [0, 1, 2]
    assert created[0]["assignees"][0]["user_id"] == str(ada.id)
    assert created[1]["status"] == "on_hold"
    assert await _titles(client, owner, org, job) == ["Prepare", "Review", "File"]
    assert len(await audit_entries(org["id"], "task.created")) == 3


async def test_one_refused_task_refuses_the_whole_batch_and_names_each(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org, start_date="2026-04-01", due_date="2026-12-31")
    await add_task(client, owner, org, job, "Existing")
    body = await _add_many(
        client,
        owner,
        org,
        job,
        [
            {"title": "Fine"},
            {"title": "Too early", "due_date": "2026-01-01"},
            {"title": "Too late", "due_date": "2027-06-01"},
            {"title": "Nobody", "assignee_user_ids": [str(uuid.uuid4())]},
        ],
        status=409,
    )
    failure = body["error"]
    assert failure["code"] == "BULK_REFUSED"
    names = [d["name"] for d in failure["details"]]
    assert names == ["Task 2: Too early", "Task 3: Too late", "Task 4: Nobody"]
    assert [d["row"] for d in failure["details"]] == [2, 3, 4]
    # Nothing was added, not even the valid first one.
    assert await _titles(client, owner, org, job) == ["Existing"]


async def test_add_many_to_a_closed_or_unknown_job(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    await set_job_row(job["id"], status="closed")
    closed = await client.post(
        org_url(org, f"/jobs/{job['id']}/tasks/bulk"),
        headers=owner.headers,
        json={"tasks": [{"title": "A"}]},
    )
    assert closed.status_code == 409 and error(closed)["code"] == "JOB_CLOSED"
    missing = await client.post(
        org_url(org, f"/jobs/{uuid.uuid4()}/tasks/bulk"),
        headers=owner.headers,
        json={"tasks": [{"title": "A"}]},
    )
    assert missing.status_code == 404


async def test_add_many_validation_and_permission(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    url = org_url(org, f"/jobs/{job['id']}/tasks/bulk")
    for body in ({"tasks": []}, {"tasks": [{"title": ""}]}, {"tasks": [{"title": "x"}] * 101}, {}):
        assert (await client.post(url, headers=owner.headers, json=body)).status_code == 422
    reader = await role_without(org, "job.update")
    forbidden = await client.post(url, headers=reader.headers, json={"tasks": [{"title": "A"}]})
    assert forbidden.status_code == 403


# --- Edit and delete many -----------------------------------------------------------------


async def _tasks(client: AsyncClient, owner, org):
    first, second = await _job(client, owner, org, "One"), await _job(client, owner, org, "Two")
    a = await add_task(client, owner, org, first, "A")
    b = await add_task(client, owner, org, first, "B")
    c = await add_task(client, owner, org, second, "C")
    return first, second, a, b, c


async def test_set_status_across_jobs_and_skip_the_ones_already_there(client: AsyncClient) -> None:
    owner, org = await practice(client)
    _, _, a, b, c = await _tasks(client, owner, org)
    ids = [a["id"], b["id"], c["id"]]
    done = await _bulk(client, owner, org, ids, "set_status", value="completed")
    assert done == {"action": "set_status", "affected": 3, "changed": 3}
    rows = await audit_entries(org["id"], "task.status_changed")
    assert len(rows) == 3
    again = await _bulk(client, owner, org, ids, "set_status", value="completed")
    assert again == {"action": "set_status", "affected": 3, "changed": 0}
    [summary] = await audit_entries(org["id"], "task.bulk_changed")
    assert summary["metadata"]["action"] == "set_status" and summary["metadata"]["count"] == 3
    assert summary["metadata"]["bulk"] is True


async def test_set_and_clear_the_due_date(client: AsyncClient) -> None:
    owner, org = await practice(client)
    first, _, a, b, _ = await _tasks(client, owner, org)
    set_ = await _bulk(client, owner, org, [a["id"], b["id"]], "set_due_date", value="2026-11-30")
    assert set_["changed"] == 2
    listed = ok_data(await client.get(task_url(org, first["id"]), headers=owner.headers))
    assert {t["due_date"] for t in listed} == {"2026-11-30"}
    cleared = await client.post(
        org_url(org, "/tasks/bulk"),
        headers=owner.headers,
        json={"task_ids": [a["id"], b["id"]], "action": "set_due_date", "value": None},
    )
    assert ok_data(cleared)["changed"] == 2
    listed = ok_data(await client.get(task_url(org, first["id"]), headers=owner.headers))
    assert {t["due_date"] for t in listed} == {None}


async def test_add_and_remove_an_assignee(client: AsyncClient) -> None:
    owner, org = await practice(client)
    first, _, a, b, _ = await _tasks(client, owner, org)
    ada = await staff(org, full_name="Ada Lovelace")
    added = await _bulk(client, owner, org, [a["id"], b["id"]], "add_assignee", value=str(ada.id))
    assert added["changed"] == 2
    again = await _bulk(client, owner, org, [a["id"], b["id"]], "add_assignee", value=str(ada.id))
    assert again["changed"] == 0
    listed = ok_data(await client.get(task_url(org, first["id"]), headers=owner.headers))
    assert all([p["user_id"] for p in t["assignees"]] == [str(ada.id)] for t in listed)
    removed = await _bulk(client, owner, org, [a["id"]], "remove_assignee", value=str(ada.id))
    assert removed["changed"] == 1
    listed = ok_data(await client.get(task_url(org, first["id"]), headers=owner.headers))
    by_title = {t["title"]: t["assignees"] for t in listed}
    assert by_title["A"] == [] and len(by_title["B"]) == 1


async def test_delete_many_and_a_task_with_time_stops_the_batch(client: AsyncClient) -> None:
    owner, org = await practice(client)
    first, _, a, b, c = await _tasks(client, owner, org)
    await log_on_job(client, owner, org, first, task_id=b["id"])
    refused = await _bulk(client, owner, org, [a["id"], b["id"], c["id"]], "delete", status=409)
    assert refused["code"] == "BULK_REFUSED"
    [only] = refused["details"]
    assert only["name"] == "B" and only["error_code"] == "TASK_IN_USE"
    assert await _titles(client, owner, org, first) == ["A", "B"]
    assert await audit_entries(org["id"], "task.deleted") == []

    result = await _bulk(client, owner, org, [a["id"], c["id"]], "delete")
    assert result == {"action": "delete", "affected": 2, "changed": 2}
    assert await _titles(client, owner, org, first) == ["B"]
    assert len(await audit_entries(org["id"], "task.deleted")) == 2


async def test_a_closed_job_refuses_its_tasks_and_nothing_changes(client: AsyncClient) -> None:
    owner, org = await practice(client)
    first, second, a, _, c = await _tasks(client, owner, org)
    await set_job_row(second["id"], status="closed")
    refused = await _bulk(
        client, owner, org, [a["id"], c["id"]], "set_status", status=409, value="completed"
    )
    assert refused["code"] == "BULK_REFUSED"
    assert [d["name"] for d in refused["details"]] == ["C"]
    assert [d["error_code"] for d in refused["details"]] == ["JOB_CLOSED"]
    listed = ok_data(await client.get(task_url(org, first["id"]), headers=owner.headers))
    assert {t["status"] for t in listed} == {"not_started"}


async def test_unknown_ids_validation_and_permissions(client: AsyncClient) -> None:
    owner, org = await practice(client)
    other_owner, other_org = await practice(client)
    _, _, a, _, _ = await _tasks(client, owner, org)
    _, _, theirs, _, _ = await _tasks(client, other_owner, other_org)
    missing = str(uuid.uuid4())
    response = await _bulk(
        client, owner, org, [a["id"], theirs["id"], missing], "delete", status=404
    )
    assert sorted(response["details"]["task_ids"]) == sorted([theirs["id"], missing])

    url = org_url(org, "/tasks/bulk")
    for body in (
        {"task_ids": [a["id"]], "action": "set_status"},
        {"task_ids": [a["id"]], "action": "set_status", "value": "done"},
        {"task_ids": [a["id"]], "action": "set_due_date", "value": "soon"},
        {"task_ids": [a["id"]], "action": "add_assignee"},
        {"task_ids": [a["id"]], "action": "add_assignee", "value": "nobody"},
        {"task_ids": [a["id"]], "action": "delete", "value": "x"},
        {"task_ids": [a["id"]], "action": "explode"},
        {"task_ids": [], "action": "delete"},
        {"task_ids": [str(uuid.uuid4())] * 201, "action": "delete"},
    ):
        assert (await client.post(url, headers=owner.headers, json=body)).status_code == 422, body

    reader = await role_without(org, "job.update")
    # Without job.read the route refuses; with it, edits need job.update.
    assert (
        await client.post(
            url, headers=reader.headers, json={"task_ids": [a["id"]], "action": "delete"}
        )
    ).status_code == 403

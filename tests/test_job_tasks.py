"""Tasks: the work inside a job (CRUD, status, assignees, order, the cross-job list, access)."""

import asyncio
import uuid
from datetime import date

import pytest
from httpx import AsyncClient

from tests.domain_factories import make_client, practice, staff
from tests.factories import audit_entries
from tests.job_helpers import add_task, create_job, job_url, role_without, set_job_row, task_url
from tests.time_gap_helpers import pin_today
from tests.time_helpers import err, ok_data, org_url


async def _job(client: AsyncClient, owner, org, **body):
    return await create_job(client, owner, org, await make_client(org), "Accounts", **body)


async def test_add_and_list_tasks(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    pin_today(monkeypatch, date(2026, 5, 1))
    owner, org = await practice(client)
    job = await _job(client, owner, org, start_date="2026-04-01", due_date="2026-12-31")
    ada = await staff(org, full_name="Ada Lovelace")
    first = await add_task(
        client,
        owner,
        org,
        job,
        "Prepare the accounts",
        description="Draft and review",
        budget_minutes=600,
        due_date="2026-06-30",
        assignee_user_ids=[ada.id],
    )
    second = await add_task(client, owner, org, job, "File at Companies House", status="on_hold")
    assert first["title"] == "Prepare the accounts" and first["status"] == "not_started"
    assert (first["sort_order"], second["sort_order"]) == (0, 1)
    assert first["description"] == "Draft and review"
    assert first["budget_minutes"] == 600 and first["budget_used_bp"] == 0
    assert first["logged_minutes"] == 0 and first["due_date"] == "2026-06-30"
    assert first["assignees"] == [{"user_id": str(ada.id), "full_name": "Ada Lovelace"}]
    assert first["completed_at"] is None and first["is_overdue"] is False
    assert first["created_by"] == str(owner.id) and first["job_id"] == job["id"]
    assert second["status"] == "on_hold" and second["assignees"] == []
    assert second["budget_used_bp"] is None

    listed = ok_data(await client.get(task_url(org, job["id"]), headers=owner.headers))
    assert [t["title"] for t in listed] == ["Prepare the accounts", "File at Companies House"]
    only_hold = ok_data(
        await client.get(
            task_url(org, job["id"]), headers=owner.headers, params={"status": "on_hold"}
        )
    )
    assert [t["title"] for t in only_hold] == ["File at Companies House"]

    read = ok_data(await client.get(job_url(org, job["id"]), headers=owner.headers))
    assert read["task_counts"] == {"total": 2, "completed": 0}
    assert (await audit_entries(org["id"], "task.created"))[0]["metadata"] == {
        "title": "Prepare the accounts",
        "job_id": job["id"],
        "job_number": job["number"],
        "job_name": "Accounts",
        "client_id": job["client_id"],
        "client_name": job["client_name"],
        "status": "not_started",
        "budget_minutes": 600,
        "due_date": "2026-06-30",
        "assignee_user_ids": [str(ada.id)],
    }


async def test_a_task_created_completed_has_its_completion_time(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    done = await add_task(client, owner, org, job, "Already done", status="completed")
    assert done["status"] == "completed" and done["completed_at"] is not None
    read = ok_data(await client.get(job_url(org, job["id"]), headers=owner.headers))
    assert read["task_counts"] == {"total": 1, "completed": 1}


async def test_validation(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org, start_date="2026-04-01", due_date="2026-12-31")
    removed = await staff(org, status="removed")

    async def bad(expect: int = 422, **body):
        return await add_task(client, owner, org, job, expect=expect, **body)

    await bad(title="")
    await bad(title="x" * 201)
    await add_task(client, owner, org, job, "x" * 200)
    await bad(description="d" * 2001)
    await bad(budget_minutes=-1)
    await bad(status="cancelled")
    await bad(unknown_field=1)
    await bad(due_date="not-a-date")
    early = await bad(due_date="2026-03-31")
    assert early["details"][0]["field"] == "due_date"
    assert early["details"][0]["code"] == "before_job_start"
    late = await bad(due_date="2027-01-01")
    assert late["details"][0]["code"] == "after_job_due"
    # The job's own boundaries are inside the range.
    await add_task(client, owner, org, job, "Edge 1", due_date="2026-04-01")
    await add_task(client, owner, org, job, "Edge 2", due_date="2026-12-31")
    gone = await bad(assignee_user_ids=[removed.id])
    assert gone["details"][0]["field"] == "user_ids"
    assert gone["details"][0]["user_ids"] == [str(removed.id)]
    assert (
        await client.post(task_url(org, job["id"]), headers=owner.headers, json={})
    ).status_code == 422
    # A job without dates takes any due date.
    open_job = await _job(client, owner, org)
    await add_task(client, owner, org, open_job, "Any day", due_date="2031-01-01")


async def test_the_task_limit(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    monkeypatch.setattr("app.services.job_tasks.MAX_TASKS_PER_JOB", 2)
    await add_task(client, owner, org, job, "One")
    await add_task(client, owner, org, job, "Two")
    error = await add_task(client, owner, org, job, "Three", expect=409)
    assert error["code"] == "TASK_LIMIT_REACHED"
    # Another job has its own limit.
    other = await _job(client, owner, org)
    await add_task(client, owner, org, other, "One")


async def test_parallel_adds_keep_a_clean_order(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    created = await asyncio.gather(
        *(add_task(client, owner, org, job, f"Task {n}") for n in range(8))
    )
    assert sorted(t["sort_order"] for t in created) == list(range(8))


async def test_update_a_task(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org, start_date="2026-04-01", due_date="2026-12-31")
    task = await add_task(client, owner, org, job, "Draft", description="Old", budget_minutes=60)
    url = task_url(org, job["id"], task["id"])
    data = ok_data(
        await client.patch(
            url,
            headers=owner.headers,
            json={"title": "Draft accounts", "description": "New", "budget_minutes": 120},
        )
    )
    assert data["title"] == "Draft accounts" and data["description"] == "New"
    assert data["budget_minutes"] == 120 and data["status"] == "not_started"
    # Null clears what can be empty (a blank description too); a title cannot be null.
    data = ok_data(
        await client.patch(
            url,
            headers=owner.headers,
            json={"description": "  ", "budget_minutes": None, "due_date": "2026-05-01"},
        )
    )
    assert data["description"] is None and data["budget_minutes"] is None
    assert data["due_date"] == "2026-05-01"
    data = ok_data(await client.patch(url, headers=owner.headers, json={"due_date": None}))
    assert data["due_date"] is None
    for body in ({"title": None}, {"title": ""}, {"status": "completed"}, {"job_id": job["id"]}):
        assert (await client.patch(url, headers=owner.headers, json=body)).status_code == 422
    error = err(
        await client.patch(url, headers=owner.headers, json={"due_date": "2027-02-02"}), 422
    )
    assert error["details"][0]["code"] == "after_job_due"
    # Nothing changed, nothing audited.
    before = len(await audit_entries(org["id"], "task.updated"))
    ok_data(await client.patch(url, headers=owner.headers, json={"title": "Draft accounts"}))
    assert len(await audit_entries(org["id"], "task.updated")) == before
    changes = (await audit_entries(org["id"], "task.updated"))[0]["metadata"]["changes"]
    assert changes["title"] == ["Draft", "Draft accounts"]
    assert changes["budget_minutes"] == [60, 120]


async def test_status_changes(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    task = await add_task(client, owner, org, job)
    url = task_url(org, job["id"], task["id"], "/status")

    async def move(target: str, status: int = 200):
        response = await client.post(url, headers=owner.headers, json={"status": target})
        assert response.status_code == status, response.text
        return response.json()["data"] if status == 200 else response.json()["error"]

    assert (await move("in_progress"))["completed_at"] is None
    done = await move("completed")
    assert done["status"] == "completed" and done["completed_at"] is not None
    stamp = done["completed_at"]
    assert (await move("completed"))["completed_at"] == stamp  # the same status changes nothing
    reopened = await move("on_hold")
    assert reopened["completed_at"] is None
    await move("not_started")
    await move("cancelled", 422)
    log = await audit_entries(org["id"], "task.status_changed")
    assert [(e["metadata"]["old"], e["metadata"]["new"]) for e in log] == [
        ("not_started", "in_progress"),
        ("in_progress", "completed"),
        ("completed", "on_hold"),
        ("on_hold", "not_started"),
    ]
    read = ok_data(await client.get(job_url(org, job["id"]), headers=owner.headers))
    assert read["task_counts"] == {"total": 1, "completed": 0}


async def test_an_assignee_moves_their_own_task_with_job_read_only(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    mine = await staff(org)
    other = await staff(org)
    task = await add_task(client, owner, org, job, "Mine", assignee_user_ids=[mine.id])
    notmine = await add_task(client, owner, org, job, "Not mine", assignee_user_ids=[other.id])
    url = task_url(org, job["id"], task["id"], "/status")
    response = await client.post(url, headers=mine.headers, json={"status": "in_progress"})
    assert ok_data(response)["status"] == "in_progress"
    entry = (await audit_entries(org["id"], "task.status_changed"))[0]
    assert entry["actor_user_id"] == str(mine.id)
    # Someone else's task, or a person who is not assigned, answers 403.
    forbidden = task_url(org, job["id"], notmine["id"], "/status")
    assert (
        await client.post(forbidden, headers=mine.headers, json={"status": "completed"})
    ).status_code == 403
    manager = await staff(org, "manager")
    assert (
        await client.post(forbidden, headers=manager.headers, json={"status": "completed"})
    ).status_code == 200
    # An assignee still cannot edit the task itself.
    assert (
        await client.patch(
            task_url(org, job["id"], task["id"]), headers=mine.headers, json={"title": "X"}
        )
    ).status_code == 403


async def test_assignees(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    a = await staff(org, full_name="Ann Able")
    b = await staff(org, full_name="Bob Baker")
    suspended = await staff(org, status="suspended")
    task = await add_task(client, owner, org, job, assignee_user_ids=[a.id])
    url = task_url(org, job["id"], task["id"], "/assignees")
    data = ok_data(
        await client.put(
            url, headers=owner.headers, json={"user_ids": [str(b.id), str(a.id), str(b.id)]}
        )
    )
    assert sorted(x["full_name"] for x in data["assignees"]) == ["Ann Able", "Bob Baker"]
    error = err(
        await client.put(url, headers=owner.headers, json={"user_ids": [str(suspended.id)]}), 422
    )
    assert error["details"][0]["user_ids"] == [str(suspended.id)]
    unknown = err(
        await client.put(url, headers=owner.headers, json={"user_ids": [str(uuid.uuid4())]}), 422
    )
    assert unknown["details"][0]["code"] == "not_active_member"
    data = ok_data(await client.put(url, headers=owner.headers, json={"user_ids": [str(b.id)]}))
    assert [x["full_name"] for x in data["assignees"]] == ["Bob Baker"]
    data = ok_data(await client.put(url, headers=owner.headers, json={"user_ids": []}))
    assert data["assignees"] == []
    # Putting the same people again is no change and not audited.
    ok_data(await client.put(url, headers=owner.headers, json={"user_ids": []}))
    assert len(await audit_entries(org["id"], "task.assignees_changed")) == 3
    assert (await client.put(url, headers=owner.headers, json={})).status_code == 422


async def test_reorder(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    tasks = [await add_task(client, owner, org, job, f"Task {n}") for n in range(3)]
    ids = [t["id"] for t in tasks]
    url = task_url(org, job["id"], suffix="/reorder")
    data = ok_data(
        await client.post(url, headers=owner.headers, json={"task_ids": [ids[2], ids[0], ids[1]]})
    )
    assert [t["id"] for t in data] == [ids[2], ids[0], ids[1]]
    assert [t["sort_order"] for t in data] == [0, 1, 2]
    listed = ok_data(await client.get(task_url(org, job["id"]), headers=owner.headers))
    assert [t["title"] for t in listed] == ["Task 2", "Task 0", "Task 1"]
    # The next task goes to the end.
    added = await add_task(client, owner, org, job, "Task 3")
    assert added["sort_order"] == 3
    # Every task exactly once.
    for body in (
        {"task_ids": ids[:2]},
        {"task_ids": [ids[0], ids[0], ids[1], ids[2]]},
        {"task_ids": [*ids, str(uuid.uuid4())]},
        {"task_ids": []},
    ):
        error = err(await client.post(url, headers=owner.headers, json=body), 422)
        assert error["details"] is not None
    mismatch = err(await client.post(url, headers=owner.headers, json={"task_ids": ids}), 422)
    assert mismatch["details"][0]["code"] == "task_ids_mismatch"
    assert len(await audit_entries(org["id"], "job.tasks_reordered")) == 1
    # The same order again changes nothing and is not audited.
    same = [
        t["id"] for t in ok_data(await client.get(task_url(org, job["id"]), headers=owner.headers))
    ]
    ok_data(await client.post(url, headers=owner.headers, json={"task_ids": same}))
    assert len(await audit_entries(org["id"], "job.tasks_reordered")) == 1


async def test_delete_a_task(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    task = await add_task(client, owner, org, job, "Draft", assignee_user_ids=[owner.id])
    url = task_url(org, job["id"], task["id"])
    response = await client.delete(url, headers=owner.headers)
    assert response.status_code == 200 and response.json()["data"] is None
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    assert ok_data(await client.get(task_url(org, job["id"]), headers=owner.headers)) == []
    read = ok_data(await client.get(job_url(org, job["id"]), headers=owner.headers))
    assert read["task_counts"] == {"total": 0, "completed": 0}
    deleted = (await audit_entries(org["id"], "task.deleted"))[0]
    assert deleted["target_id"] == task["id"] and deleted["metadata"]["title"] == "Draft"


async def test_tasks_of_a_closed_or_cancelled_job_cannot_change(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    task = await add_task(client, owner, org, job, "Draft")
    other = await add_task(client, owner, org, job, "Review")
    for locked in ("closed", "cancelled"):
        await set_job_row(job["id"], status=locked)
        url = task_url(org, job["id"], task["id"])
        calls = [
            ("post", task_url(org, job["id"]), {"title": "New"}),
            ("patch", url, {"title": "Renamed"}),
            ("post", url + "/status", {"status": "completed"}),
            ("put", url + "/assignees", {"user_ids": []}),
            ("delete", url, None),
            (
                "post",
                task_url(org, job["id"], suffix="/reorder"),
                {"task_ids": [task["id"], other["id"]]},
            ),
        ]
        for method, path, body in calls:
            response = await client.request(method, path, headers=owner.headers, json=body)
            assert response.status_code == 409, (locked, method, path, response.text)
            assert response.json()["error"]["code"] == "JOB_CLOSED"
        # Reading still works.
        assert len(ok_data(await client.get(task_url(org, job["id"]), headers=owner.headers))) == 2
    # On hold and completed jobs still take task changes.
    await set_job_row(job["id"], status="on_hold")
    await add_task(client, owner, org, job, "Still open")
    await set_job_row(job["id"], status="completed")
    await add_task(client, owner, org, job, "Wrap up")


async def test_a_task_belongs_to_its_job(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org)
    one = await create_job(client, owner, org, acme, "One")
    two = await create_job(client, owner, org, acme, "Two")
    task = await add_task(client, owner, org, one, "Draft")
    wrong = task_url(org, two["id"], task["id"])
    assert (
        await client.patch(wrong, headers=owner.headers, json={"title": "X"})
    ).status_code == 404
    assert (await client.delete(wrong, headers=owner.headers)).status_code == 404
    assert (
        await client.post(wrong + "/status", headers=owner.headers, json={"status": "completed"})
    ).status_code == 404
    assert (
        await client.put(wrong + "/assignees", headers=owner.headers, json={"user_ids": []})
    ).status_code == 404
    unknown = task_url(org, one["id"], uuid.uuid4())
    assert (
        await client.patch(unknown, headers=owner.headers, json={"title": "X"})
    ).status_code == 404
    deleted = await create_job(client, owner, org, acme, "Gone")
    await client.delete(job_url(org, deleted["id"]), headers=owner.headers)
    assert (
        await client.get(task_url(org, deleted["id"]), headers=owner.headers)
    ).status_code == 404
    reorder = task_url(org, two["id"], suffix="/reorder")
    error = err(
        await client.post(reorder, headers=owner.headers, json={"task_ids": [task["id"]]}), 422
    )
    assert error["details"][0]["code"] == "task_ids_mismatch"


async def test_other_practices_answer_not_found(client: AsyncClient) -> None:
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other & Co")
    theirs = await create_job(client, other_owner, other, await make_client(other), "Theirs")
    their_task = await add_task(client, other_owner, other, theirs, "Secret")
    mine = await _job(client, owner, org)
    # Their ids under our practice are not found...
    assert (await client.get(task_url(org, theirs["id"]), headers=owner.headers)).status_code == 404
    assert (
        await client.post(task_url(org, theirs["id"]), headers=owner.headers, json={"title": "X"})
    ).status_code == 404
    path = task_url(org, theirs["id"], their_task["id"])
    assert (await client.patch(path, headers=owner.headers, json={"title": "X"})).status_code == 404
    assert (await client.delete(path, headers=owner.headers)).status_code == 404
    path = task_url(org, mine["id"], their_task["id"])
    assert (await client.patch(path, headers=owner.headers, json={"title": "X"})).status_code == 404
    # ...an assignee of the other practice is no member of ours...
    stranger = await staff(other)
    error = await add_task(
        client, owner, org, mine, "X", expect=422, assignee_user_ids=[stranger.id]
    )
    assert error["details"][0]["field"] == "user_ids"
    # ...and the other practice's owner cannot reach ours at all.
    assert (
        await client.get(task_url(org, mine["id"]), headers=other_owner.headers)
    ).status_code == 404
    assert (
        await client.get(org_url(org, "/tasks"), headers=other_owner.headers)
    ).status_code == 404
    # The cross-job list shows only our own tasks.
    await add_task(client, owner, org, mine, "Ours")
    titles = [
        t["title"] for t in ok_data(await client.get(org_url(org, "/tasks"), headers=owner.headers))
    ]
    assert titles == ["Ours"]


async def test_permissions_per_role(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    task = await add_task(client, owner, org, job, "Draft")
    base = task_url(org, job["id"])
    one = task_url(org, job["id"], task["id"])
    for role in ("staff", "viewer", "billing"):
        person = await staff(org, role)
        assert (await client.get(base, headers=person.headers)).status_code == 200, role
        assert (await client.get(org_url(org, "/tasks"), headers=person.headers)).status_code == 200
        writes = [
            await client.post(base, headers=person.headers, json={"title": "X"}),
            await client.patch(one, headers=person.headers, json={"title": "X"}),
            await client.put(one + "/assignees", headers=person.headers, json={"user_ids": []}),
            await client.delete(one, headers=person.headers),
            await client.post(
                base + "/reorder", headers=person.headers, json={"task_ids": [task["id"]]}
            ),
            await client.post(
                one + "/status", headers=person.headers, json={"status": "completed"}
            ),
        ]
        assert [r.status_code for r in writes] == [403] * 6, role
    manager = await staff(org, "manager")
    assert (
        await client.post(base, headers=manager.headers, json={"title": "By manager"})
    ).status_code == 201
    assert (await client.delete(one, headers=manager.headers)).status_code == 200


async def test_a_role_without_job_read_sees_nothing(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await _job(client, owner, org)
    nobody = await role_without(org, "job.read")
    assert (await client.get(task_url(org, job["id"]), headers=nobody.headers)).status_code == 403
    assert (await client.get(org_url(org, "/tasks"), headers=nobody.headers)).status_code == 403
    assert (await client.get(task_url(org, job["id"]))).status_code == 401


async def test_the_cross_job_list(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    pin_today(monkeypatch, date(2026, 7, 1))
    owner, org = await practice(client)
    acme, beta = await make_client(org, "Acme Ltd"), await make_client(org, "Beta plc")
    j1 = await create_job(client, owner, org, acme, "Accounts")
    j2 = await create_job(client, owner, org, beta, "Payroll")
    ann = await staff(org, full_name="Ann Able")
    t1 = await add_task(
        client, owner, org, j1, "Bookkeeping", due_date="2026-08-01", assignee_user_ids=[ann.id]
    )
    t2 = await add_task(
        client, owner, org, j1, "Review", due_date="2026-06-01", status="in_progress"
    )
    t3 = await add_task(
        client, owner, org, j2, "Payslips", assignee_user_ids=[ann.id, owner.id], status="completed"
    )
    t4 = await add_task(client, owner, org, j2, "Pension", due_date="2026-09-01", status="on_hold")
    base = org_url(org, "/tasks")

    async def titles(**params) -> list[str]:
        data = ok_data(await client.get(base, headers=owner.headers, params=params))
        return [t["title"] for t in data]

    assert await titles() == ["Review", "Bookkeeping", "Pension", "Payslips"]  # due date, none last
    assert await titles(sort="-due_date") == ["Payslips", "Pension", "Bookkeeping", "Review"]
    assert await titles(sort="title") == ["Bookkeeping", "Payslips", "Pension", "Review"]
    assert await titles(sort="-title") == ["Review", "Pension", "Payslips", "Bookkeeping"]
    # By job number, then in the order the tasks were made.
    assert await titles(sort="job") == ["Bookkeeping", "Review", "Payslips", "Pension"]
    assert await titles(sort="-job") == ["Pension", "Payslips", "Review", "Bookkeeping"]
    assert await titles(sort="status") == ["Bookkeeping", "Review", "Pension", "Payslips"]
    assert await titles(sort="-status") == ["Payslips", "Pension", "Review", "Bookkeeping"]
    assert await titles(job_id=j2["id"], sort="title") == ["Payslips", "Pension"]
    assert await titles(client_id=acme.id, sort="title") == ["Bookkeeping", "Review"]
    assert await titles(assignee_user_id=ann.id, sort="title") == ["Bookkeeping", "Payslips"]
    assert await titles(assignee_user_id="me") == ["Payslips"]
    assert await titles(status="completed") == ["Payslips"]
    assert await titles(status=["in_progress", "on_hold"], sort="title") == ["Pension", "Review"]
    assert await titles(due_from="2026-07-01", due_to="2026-08-31") == ["Bookkeeping"]
    assert await titles(due_to="2026-06-30") == ["Review"]
    # "pay" also matches the job name "Payroll", which both Payslips and Pension belong to.
    assert await titles(q="pay", sort="title") == ["Payslips", "Pension"]
    assert await titles(q="Beta", sort="title") == ["Payslips", "Pension"]
    assert await titles(q=j1["number"], sort="title") == ["Bookkeeping", "Review"]
    assert await titles(q="nothing like this") == []
    listed = {t["title"]: t for t in ok_data(await client.get(base, headers=owner.headers))}
    assert listed["Review"]["is_overdue"] is True  # due before today and not completed
    assert (
        listed["Bookkeeping"]["is_overdue"] is False and listed["Payslips"]["is_overdue"] is False
    )
    assert listed["Bookkeeping"]["job_name"] == "Accounts"
    assert listed["Bookkeeping"]["job_number"] == j1["number"]
    assert listed["Bookkeeping"]["client_name"] == "Acme Ltd"
    assert listed["Bookkeeping"]["job_status"] == "planned"
    assert {t1["id"], t2["id"], t3["id"], t4["id"]} == {t["id"] for t in listed.values()}
    for params in (
        {"assignee_user_id": "nobody"},
        {"sort": "bogus"},
        {"status": "bad"},
        {"cursor": "garbage"},
    ):
        assert (await client.get(base, headers=owner.headers, params=params)).status_code == 422
    # A deleted job's tasks are not listed.
    await client.delete(job_url(org, j1["id"]), headers=owner.headers)
    assert await titles(sort="title") == ["Payslips", "Pension"]


async def test_the_cross_job_list_paginates_every_sort(client: AsyncClient) -> None:
    owner, org = await practice(client)
    a, b = await make_client(org, "Acme Ltd"), await make_client(org, "Beta plc")
    j1, j2 = (
        await create_job(client, owner, org, a, "One"),
        await create_job(client, owner, org, b, "Two"),
    )
    # Some tasks without a due date, some with, in two statuses: ties everywhere.
    for n in range(4):
        due = None if n == 2 else f"2026-0{n + 1}-01"
        status = "in_progress" if n % 2 else "not_started"
        await add_task(client, owner, org, j1, f"Task {n}", due_date=due, status=status)
    for n in range(3):
        await add_task(client, owner, org, j2, f"Other {n}", due_date="2026-02-01")
    base = org_url(org, "/tasks")
    for sort in ("due_date", "-due_date", "title", "-title", "job", "-job", "status", "-status"):
        everything = [
            t["id"]
            for t in ok_data(
                await client.get(base, headers=owner.headers, params={"sort": sort, "limit": 100})
            )
        ]
        seen: list[str] = []
        cursor = None
        while True:
            params = {"sort": sort, "limit": 2, **({"cursor": cursor} if cursor else {})}
            body = (await client.get(base, headers=owner.headers, params=params)).json()
            seen += [t["id"] for t in body["data"]]
            cursor = body["meta"]["next_cursor"]
            if not cursor:
                break
        assert seen == everything, sort
        assert len(set(seen)) == 7

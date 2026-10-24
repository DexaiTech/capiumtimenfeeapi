"""Service task templates: a service's default tasks, added to a new job automatically or to an
existing job on request."""

import uuid

import pytest
from httpx import AsyncClient

from tests.clients_helpers import sql
from tests.domain_factories import make_client, practice, staff
from tests.factories import add_member, audit_entries, error
from tests.job_helpers import add_task, create_job, set_job_row, task_url
from tests.time_helpers import ok_data, org_url


def _templates_url(org: dict, service_id: str) -> str:
    return org_url(org, f"/services/{service_id}/task-templates")


async def _service(client: AsyncClient, owner, org, code="ACC", **body) -> dict:
    response = await client.post(
        org_url(org, "/services"),
        headers=owner.headers,
        json={"code": code, "name": f"Service {code}", **body},
    )
    return ok_data(response, 201)


async def _set(client: AsyncClient, owner, org, service, templates, status=200):
    response = await client.put(
        _templates_url(org, service["id"]), headers=owner.headers, json={"templates": templates}
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status == 200 else response.json()["error"]


async def _tasks(client: AsyncClient, owner, org, job) -> list[dict]:
    return ok_data(await client.get(task_url(org, job["id"]), headers=owner.headers))


THREE = [
    {"title": "Prepare the accounts", "budget_minutes": 600, "due_offset_days": 14},
    {
        "title": "Review with the partner",
        "description": "Second pair of eyes",
        "due_offset_days": 30,
    },
    {"title": "File at Companies House"},
]


# --- The templates ------------------------------------------------------------------------


async def test_set_list_and_replace_the_default_tasks(client: AsyncClient) -> None:
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    assert service["task_template_count"] == 0
    assert (
        ok_data(await client.get(_templates_url(org, service["id"]), headers=owner.headers)) == []
    )

    saved = await _set(client, owner, org, service, THREE)
    assert [t["title"] for t in saved] == [t["title"] for t in THREE]
    assert [t["sort_order"] for t in saved] == [0, 1, 2]
    assert saved[0]["budget_minutes"] == 600 and saved[0]["due_offset_days"] == 14
    assert saved[1]["description"] == "Second pair of eyes" and saved[2]["due_offset_days"] is None
    one = ok_data(
        await client.get(org_url(org, f"/services/{service['id']}"), headers=owner.headers)
    )
    assert one["task_template_count"] == 3
    listed = ok_data(await client.get(org_url(org, "/services"), headers=owner.headers))
    assert {s["code"]: s["task_template_count"] for s in listed}["ACC"] == 3

    # Replaced as a whole, in the order sent.
    again = await _set(client, owner, org, service, [{"title": "Only one"}, {"title": "And two"}])
    assert [t["title"] for t in again] == ["Only one", "And two"]
    [first, second] = await audit_entries(org["id"], "service.task_templates_changed")
    assert {first["metadata"]["count"], second["metadata"]["count"]} == {2, 3}
    cleared = await _set(client, owner, org, service, [])
    assert cleared == []


async def test_saving_the_same_list_changes_and_audits_nothing(client: AsyncClient) -> None:
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    await _set(client, owner, org, service, THREE)
    await _set(client, owner, org, service, THREE)
    assert len(await audit_entries(org["id"], "service.task_templates_changed")) == 1


@pytest.mark.parametrize(
    "templates",
    [
        [{"title": ""}],
        [{"title": "   "}],
        [{"title": "x" * 201}],
        [{"title": "ok", "budget_minutes": -1}],
        [{"title": "ok", "due_offset_days": -1}],
        [{"title": "ok", "due_offset_days": 3651}],
        [{"title": "ok", "description": "x" * 2001}],
        [{"title": "ok"}] * 101,
        [{"title": "ok", "colour": "red"}],
    ],
)
async def test_the_list_is_validated(client: AsyncClient, templates: list) -> None:
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    response = await client.put(
        _templates_url(org, service["id"]), headers=owner.headers, json={"templates": templates}
    )
    assert response.status_code == 422, response.text


async def test_titles_are_tidied_and_blank_descriptions_dropped(client: AsyncClient) -> None:
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    saved = await _set(
        client, owner, org, service, [{"title": "  Prepare   the  accounts ", "description": "  "}]
    )
    assert saved[0]["title"] == "Prepare the accounts" and saved[0]["description"] is None


async def test_permissions_and_other_practices(client: AsyncClient) -> None:
    owner, org = await practice(client)
    other_owner, other_org = await practice(client)
    service = await _service(client, owner, org)
    theirs = await _service(client, other_owner, other_org, code="OTH")
    staff_member = await add_member(org["id"], "staff")
    # Any member may read the default tasks; changing them needs settings.manage.
    assert (
        await client.get(_templates_url(org, service["id"]), headers=staff_member.headers)
    ).status_code == 200
    refused = await client.put(
        _templates_url(org, service["id"]), headers=staff_member.headers, json={"templates": []}
    )
    assert refused.status_code == 403
    # Another practice's service is not found, for reading and for writing.
    assert (
        await client.get(_templates_url(org, theirs["id"]), headers=owner.headers)
    ).status_code == 404
    assert (
        await client.put(
            _templates_url(org, theirs["id"]), headers=owner.headers, json={"templates": []}
        )
    ).status_code == 404
    assert (
        await client.get(_templates_url(org, str(uuid.uuid4())), headers=owner.headers)
    ).status_code == 404


async def test_deleting_an_unused_service_removes_its_templates(client: AsyncClient) -> None:
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    await _set(client, owner, org, service, THREE)
    deleted = await client.delete(org_url(org, f"/services/{service['id']}"), headers=owner.headers)
    assert deleted.status_code == 200

    assert (
        await sql("SELECT 1 FROM service_task_templates WHERE service_id = :s", s=service["id"])
        == []
    )


# --- A new job ------------------------------------------------------------------------------


async def test_a_new_job_gets_the_default_tasks_when_asked(client: AsyncClient) -> None:
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    await _set(client, owner, org, service, THREE)
    ada = await staff(org, full_name="Ada Lovelace")
    job = await create_job(
        client,
        owner,
        org,
        await make_client(org),
        "Year end",
        service_id=service["id"],
        start_date="2026-04-01",
        due_date="2026-12-31",
        assignee_user_ids=[ada.id],
        add_default_tasks=True,
    )
    tasks = await _tasks(client, owner, org, job)
    assert [t["title"] for t in tasks] == [t["title"] for t in THREE]
    assert [t["sort_order"] for t in tasks] == [0, 1, 2]
    assert [t["due_date"] for t in tasks] == ["2026-04-15", "2026-05-01", None]
    assert tasks[0]["budget_minutes"] == 600 and tasks[1]["description"] == "Second pair of eyes"
    assert {t["status"] for t in tasks} == {"not_started"}
    for task in tasks:
        assert [a["user_id"] for a in task["assignees"]] == [str(ada.id)]
    read = ok_data(await client.get(org_url(org, f"/jobs/{job['id']}"), headers=owner.headers))
    assert read["task_counts"] == {"total": 3, "completed": 0}
    created = await audit_entries(org["id"], "task.created")
    assert len(created) == 3 and all(e["metadata"]["source"] == "service_template" for e in created)
    [job_entry] = await audit_entries(org["id"], "job.created")
    assert job_entry["metadata"]["default_tasks_added"] == 3


async def test_a_new_job_has_no_tasks_unless_asked(client: AsyncClient) -> None:
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    await _set(client, owner, org, service, THREE)
    customer = await make_client(org)
    plain = await create_job(client, owner, org, customer, "Plain", service_id=service["id"])
    assert await _tasks(client, owner, org, plain) == []
    no_service = await create_job(
        client, owner, org, customer, "No service", add_default_tasks=True
    )
    assert await _tasks(client, owner, org, no_service) == []
    empty = await _service(client, owner, org, code="EMPTY")
    nothing = await create_job(
        client, owner, org, customer, "Nothing", service_id=empty["id"], add_default_tasks=True
    )
    assert await _tasks(client, owner, org, nothing) == []


async def test_due_dates_follow_the_job_and_never_fail_it(client: AsyncClient) -> None:
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    await _set(client, owner, org, service, THREE)
    customer = await make_client(org)
    # No start date: no due dates.
    undated = await create_job(
        client, owner, org, customer, "A", service_id=service["id"], add_default_tasks=True
    )
    assert [t["due_date"] for t in await _tasks(client, owner, org, undated)] == [None] * 3
    # The 30-day task would fall after the job's due date: that one gets none.
    short = await create_job(
        client,
        owner,
        org,
        customer,
        "B",
        service_id=service["id"],
        start_date="2026-04-01",
        due_date="2026-04-20",
        add_default_tasks=True,
    )
    assert [t["due_date"] for t in await _tasks(client, owner, org, short)] == [
        "2026-04-15",
        None,
        None,
    ]


async def test_the_task_limit_stops_the_job_and_nothing_is_created(client: AsyncClient) -> None:
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    await _set(client, owner, org, service, [{"title": f"T{n}"} for n in range(3)])
    customer = await make_client(org)
    from app.services import job_tasks

    original = job_tasks.MAX_TASKS_PER_JOB
    job_tasks.MAX_TASKS_PER_JOB = 2
    try:
        response = await client.post(
            org_url(org, "/jobs"),
            headers=owner.headers,
            json={
                "client_id": str(customer.id),
                "name": "Too many",
                "service_id": service["id"],
                "add_default_tasks": True,
            },
        )
    finally:
        job_tasks.MAX_TASKS_PER_JOB = original
    assert response.status_code == 409 and error(response)["code"] == "TASK_LIMIT_REACHED"
    listed = ok_data(await client.get(org_url(org, "/jobs"), headers=owner.headers))
    assert listed == []


# --- An existing job ------------------------------------------------------------------------


async def test_add_the_default_tasks_to_an_existing_job_once(client: AsyncClient) -> None:
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    await _set(client, owner, org, service, THREE)
    job = await create_job(
        client,
        owner,
        org,
        await make_client(org),
        "Existing",
        service_id=service["id"],
        start_date="2026-04-01",
    )
    await add_task(client, owner, org, job, "prepare the accounts")  # same title, other case
    url = org_url(org, f"/jobs/{job['id']}/tasks/from-service")
    first = await client.post(url, headers=owner.headers)
    assert ok_data(first) == {"added": 2, "skipped": 1}
    titles = [t["title"] for t in await _tasks(client, owner, org, job)]
    assert titles == ["prepare the accounts", "Review with the partner", "File at Companies House"]
    again = await client.post(url, headers=owner.headers)
    assert ok_data(again) == {"added": 0, "skipped": 3}
    assert len(await _tasks(client, owner, org, job)) == 3


async def test_from_service_refusals_and_permission(client: AsyncClient) -> None:
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    customer = await make_client(org)
    no_templates = await create_job(client, owner, org, customer, "A", service_id=service["id"])
    no_service = await create_job(client, owner, org, customer, "B")
    for job in (no_templates, no_service):
        response = await client.post(
            org_url(org, f"/jobs/{job['id']}/tasks/from-service"), headers=owner.headers
        )
        assert response.status_code == 409 and error(response)["code"] == "SERVICE_HAS_NO_TASKS"
    await _set(client, owner, org, service, THREE)
    closed = await create_job(client, owner, org, customer, "C", service_id=service["id"])
    await set_job_row(closed["id"], status="closed")
    closed_response = await client.post(
        org_url(org, f"/jobs/{closed['id']}/tasks/from-service"), headers=owner.headers
    )
    assert closed_response.status_code == 409 and error(closed_response)["code"] == "JOB_CLOSED"
    missing = await client.post(
        org_url(org, f"/jobs/{uuid.uuid4()}/tasks/from-service"), headers=owner.headers
    )
    assert missing.status_code == 404
    reader = await add_member(org["id"], "viewer")
    forbidden = await client.post(
        org_url(org, f"/jobs/{no_templates['id']}/tasks/from-service"), headers=reader.headers
    )
    assert forbidden.status_code == 403


async def test_copying_a_job_is_unchanged(client: AsyncClient) -> None:
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    await _set(client, owner, org, service, THREE)
    job = await create_job(
        client, owner, org, await make_client(org), "Source", service_id=service["id"]
    )
    await add_task(client, owner, org, job, "Only task")
    copied = ok_data(
        await client.post(org_url(org, f"/jobs/{job['id']}/copy"), headers=owner.headers, json={}),
        201,
    )
    assert [t["title"] for t in await _tasks(client, owner, org, copied)] == ["Only task"]

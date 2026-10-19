"""The audit log read side: the list (with its filters and the normalised changes) and the history
of one record (permissions by record type, related entries, pagination, redaction)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.audit_helpers import audit_url, history, insert_entry, listing
from tests.clients_helpers import create_client, org_url
from tests.domain_factories import make_client, make_job, practice, staff
from tests.factories import add_member, create_user, error, role_id
from tests.foundation_helpers import add_custom_role
from tests.team_helpers import new_rate
from tests.time_helpers import log_time

ENTRY_KEYS = {
    "id",
    "action",
    "label",
    "actor",
    "at",
    "ip_address",
    "target_type",
    "target_id",
    "changes",
    "summary",
}
NOW = datetime.now(UTC)


def minutes_ago(count: int) -> datetime:
    return NOW - timedelta(minutes=count)


def actions(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [entry["action"] for entry in response.json()["data"]]


async def reader(org: dict, name: str = "Auditor", *extra: str):
    """A member whose custom role holds ``audit_log.read`` and ``extra``, nothing else."""
    await add_custom_role(org["id"], name, ["audit_log.read", *extra])
    return await staff(org, name)


# --- The history of a record ------------------------------------------------------------------


async def test_a_clients_history_is_newest_first_and_normalised(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, "Acme Ltd")
    patched = await client.patch(
        org_url(org, f"/clients/{acme['id']}"), headers=owner.headers, json={"name": "Acme Group"}
    )
    assert patched.status_code == 200, patched.text

    response = await history(client, owner, org, "client", acme["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"] == {"next_cursor": None, "has_more": False}
    updated, created = body["data"]
    assert set(updated) == ENTRY_KEYS
    assert updated["action"] == "client.updated" and updated["label"] == "Client details updated"
    assert updated["changes"] == [{"field": "name", "before": "Acme Ltd", "after": "Acme Group"}]
    assert updated["actor"] == {"id": str(owner.id), "name": owner.full_name}
    assert (updated["target_type"], updated["target_id"]) == ("client", acme["id"])
    assert updated["ip_address"] == "127.0.0.1"
    assert updated["summary"] == f"Client details updated: {acme['code']} · Acme Group"
    assert datetime.fromisoformat(updated["at"]) >= datetime.fromisoformat(created["at"])
    assert created["action"] == "client.created" and created["changes"] == []
    assert created["summary"] == f"Client created: {acme['code']} · Acme Ltd"


async def test_changes_are_normalised_from_every_shape_the_services_write(client):
    owner, org = await practice(client)
    person = await staff(org, full_name="Pat Person")
    manager = await staff(org, "manager")
    acme = await make_client(org, "Acme Ltd")

    # old/new plain values: a job's status.
    job = await make_job(org, acme, "Accounts", status="planned")
    moved = await client.post(
        org_url(org, f"/jobs/{job.id}/status"), headers=owner.headers, json={"status": "on_hold"}
    )
    assert moved.status_code == 200, moved.text
    [entry] = (await history(client, owner, org, "job", job.id)).json()["data"]
    assert entry["changes"] == [{"field": "status", "before": "planned", "after": "on_hold"}]
    assert entry["label"] == "Job status changed"
    assert entry["summary"] == f"Job status changed: {job.number} · Accounts (Acme Ltd)"

    # old_status/new_status pairs: a timesheet.
    logged = await log_time(client, person, org)
    sheet_url = org_url(org, f"/timesheets/{logged['timesheet_id']}")
    assert (await client.post(f"{sheet_url}/submit", headers=person.headers)).status_code == 200
    assert (await client.post(f"{sheet_url}/approve", headers=manager.headers)).status_code == 200
    approved, submitted = (
        await history(client, owner, org, "timesheet", logged["timesheet_id"])
    ).json()["data"]
    assert approved["changes"] == [{"field": "status", "before": "submitted", "after": "approved"}]
    assert submitted["changes"] == [{"field": "status", "before": "open", "after": "submitted"}]
    assert approved["actor"]["id"] == str(manager.id)

    # old/new objects: a member's role (the id gives way to the name).
    changed = await client.patch(
        org_url(org, f"/members/{person.id}"),
        headers=owner.headers,
        json={"role_id": await role_id(org["id"], "manager")},
    )
    assert changed.status_code == 200, changed.text
    [entry] = (await history(client, owner, org, "member", person.id)).json()["data"]
    assert entry["action"] == "member.role_changed"
    assert entry["changes"] == [
        {"field": "role_name", "before": "Staff", "after": "Manager"},
        {"field": "system_key", "before": "staff", "after": "manager"},
    ]


async def test_a_creation_or_deletion_has_no_changes_but_a_summary(client):
    owner, org = await practice(client)
    invoice_id = uuid.uuid4()
    await insert_entry(
        org,
        "invoice.issued",
        actor=owner,
        target_type="invoice",
        target_id=invoice_id,
        meta={"number": "INV-00012", "client_name": "Babbage Ltd", "gross_total": 120000},
    )
    [entry] = (await history(client, owner, org, "invoice", invoice_id)).json()["data"]
    assert entry["changes"] == []
    assert entry["label"] == "Invoice issued"
    assert entry["summary"] == "Invoice issued: INV-00012 (Babbage Ltd)"


async def test_an_action_without_a_label_gets_one_made_from_it(client):
    owner, org = await practice(client)
    record = uuid.uuid4()
    await insert_entry(org, "widget_part.re_balanced", target_type="widget", target_id=record)
    [entry] = (await history(client, owner, org, "widget", record)).json()["data"]
    assert entry["label"] == "Widget part re balanced"
    assert entry["summary"] == "Widget part re balanced"
    assert entry["actor"] is None and entry["ip_address"] == "203.0.113.7"


# --- Who may read a record's history ----------------------------------------------------------


async def test_the_record_read_permission_opens_that_types_history(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, "Acme Ltd")
    viewer = await staff(org, "viewer")
    member = await staff(org, "staff")
    await add_custom_role(org["id"], "Nothing", ["member.read"])
    nobody = await staff(org, "Nothing")

    for person in (viewer, member):
        response = await history(client, person, org, "client", acme["id"])
        assert response.status_code == 200, response.text
        [entry] = response.json()["data"]
        assert entry["action"] == "client.created"
        # The address is for people who read the audit log.
        assert entry["ip_address"] is None
        assert entry["actor"]["name"] == owner.full_name
    assert (await history(client, owner, org, "client", acme["id"])).json()["data"][0][
        "ip_address"
    ] == "127.0.0.1"

    denied = await history(client, nobody, org, "client", acme["id"])
    assert denied.status_code == 403
    assert error(denied)["code"] == "PERMISSION_DENIED"
    # Whatever the id: the permission is checked before the record is looked for.
    assert (await history(client, nobody, org, "client", uuid.uuid4())).status_code == 403


@pytest.mark.parametrize(
    ("role", "record_type", "status"),
    [
        ("viewer", "invoice", 200),  # invoice.read
        ("viewer", "payment", 200),  # payment.read
        ("viewer", "job", 200),  # job.read
        ("viewer", "time_entry", 200),  # time.read_all
        ("viewer", "rate_rule", 403),  # rate.read is not a viewer's
        ("staff", "invoice", 403),
        ("staff", "payment", 403),
        ("staff", "time_entry", 403),
        ("staff", "job", 200),
        ("manager", "rate_rule", 200),
        ("manager", "period_lock", 200),
        ("billing", "expense_claim", 200),  # expense.read_all
        ("billing", "rate_rule", 403),
        # Types without a record permission (staff, roles, settings, anything unknown).
        ("viewer", "member", 403),
        ("manager", "role", 403),
        ("viewer", "organization", 403),
        ("viewer", "something_new", 403),
        ("partner", "member", 200),  # audit_log.read reads every type
        ("partner", "something_new", 200),
        ("owner", "organization", 200),
    ],
)
async def test_the_mapping_of_record_types_to_permissions(client, role, record_type, status):
    owner, org = await practice(client)
    caller = owner if role == "owner" else await staff(org, role)
    response = await history(client, caller, org, record_type, uuid.uuid4())
    assert response.status_code == status, response.text


async def test_the_audit_log_permission_alone_reads_every_history(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, "Acme Ltd")
    auditor = await reader(org)
    response = await history(client, auditor, org, "client", acme["id"])
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["ip_address"] == "127.0.0.1"


# --- Related entries --------------------------------------------------------------------------


async def test_a_clients_history_can_include_what_names_the_client(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, "Acme Ltd")
    other = await create_client(client, owner, org, "Other Ltd")
    named = {"client_id": acme["id"], "client_name": "Acme Ltd"}
    await insert_entry(
        org,
        "invoice.issued",
        actor=owner,
        target_type="invoice",
        target_id=uuid.uuid4(),
        meta={**named, "number": "INV-1"},
    )
    await insert_entry(
        org,
        "client_rate.created",
        actor=owner,
        target_type="client_rate",
        target_id=uuid.uuid4(),
        meta=named,
    )
    await insert_entry(
        org,
        "invoice.issued",
        actor=owner,
        target_type="invoice",
        target_id=uuid.uuid4(),
        meta={"client_id": other["id"], "number": "INV-2"},
    )
    await insert_entry(org, "audit.mystery", actor=owner, target_type="mystery", meta=named)

    plain = await history(client, owner, org, "client", acme["id"])
    assert actions(plain) == ["client.created"]
    related = await history(client, owner, org, "client", acme["id"], include_related="true")
    assert sorted(actions(related)) == [
        "audit.mystery",
        "client.created",
        "client_rate.created",
        "invoice.issued",
    ]
    assert all(entry["target_id"] != other["id"] for entry in related.json()["data"]), (
        "another client's entries never come along"
    )

    # Without audit_log.read only the types the caller may read come along.
    viewer = await staff(org, "viewer")
    seen = await history(client, viewer, org, "client", acme["id"], include_related="true")
    assert sorted(actions(seen)) == ["client.created", "invoice.issued"]
    manager = await staff(org, "manager")
    seen = await history(client, manager, org, "client", acme["id"], include_related="true")
    assert sorted(actions(seen)) == ["client.created", "client_rate.created", "invoice.issued"]


async def test_only_clients_have_related_entries(client):
    owner, org = await practice(client)
    job_id = uuid.uuid4()
    await insert_entry(org, "job.created", target_type="job", target_id=job_id)
    await insert_entry(
        org,
        "task.created",
        target_type="task",
        target_id=uuid.uuid4(),
        meta={"job_id": str(job_id)},
    )
    response = await history(client, owner, org, "job", job_id, include_related="true")
    assert actions(response) == ["job.created"]


# --- Pagination, filters and validation --------------------------------------------------------


async def test_the_history_is_paginated_with_a_cursor(client):
    owner, org = await practice(client)
    record = uuid.uuid4()
    for index in range(7):
        await insert_entry(
            org,
            "job.updated",
            target_type="job",
            target_id=record,
            created_at=minutes_ago(index),
            meta={"changes": {"name": [f"v{index}", f"v{index + 1}"]}},
        )
    seen = []
    cursor = None
    pages = []
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        response = await history(client, owner, org, "job", record, **params)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append((len(body["data"]), body["meta"]["has_more"]))
        seen += [entry["changes"][0]["before"] for entry in body["data"]]
        cursor = body["meta"]["next_cursor"]
        if not cursor:
            break
    assert pages == [(3, True), (3, True), (1, False)]
    assert seen == [f"v{index}" for index in range(7)]  # newest first, none twice


async def test_the_history_takes_the_list_filters(client):
    owner, org = await practice(client)
    other = await staff(org, "manager")
    record = uuid.uuid4()
    await insert_entry(
        org,
        "job.created",
        actor=owner,
        target_type="job",
        target_id=record,
        created_at=minutes_ago(60),
        meta={"name": "Accounts"},
    )
    await insert_entry(
        org,
        "job.updated",
        actor=other,
        target_type="job",
        target_id=record,
        created_at=minutes_ago(30),
        meta={"name": "Accounts", "changes": {"name": ["a", "b"]}},
    )
    await insert_entry(
        org,
        "job.status_changed",
        actor=owner,
        target_type="job",
        target_id=record,
        created_at=minutes_ago(5),
        meta={"old": "planned", "new": "in_progress"},
    )
    await insert_entry(org, "job.updated", actor=owner, target_type="job", target_id=uuid.uuid4())

    async def ask(**params):
        return actions(await history(client, owner, org, "job", record, **params))

    assert await ask() == ["job.status_changed", "job.updated", "job.created"]
    assert await ask(action="job.updated") == ["job.updated"]
    assert await ask(action="job.") == ["job.status_changed", "job.updated", "job.created"]
    assert await ask(actor_user_id=str(other.id)) == ["job.updated"]
    assert await ask(**{"from": minutes_ago(45).isoformat()}) == [
        "job.status_changed",
        "job.updated",
    ]
    assert await ask(to=minutes_ago(45).isoformat()) == ["job.created"]
    assert await ask(q="in_progress") == ["job.status_changed"]
    assert await ask(q=other.full_name.lower()) == ["job.updated"]
    assert await ask(q="no such thing") == []


@pytest.mark.parametrize(
    "params",
    [
        {},  # no type, no id
        {"target_type": "client"},
        {"target_id": str(uuid.uuid4())},
        {"target_type": "client", "target_id": "not-a-uuid"},
        {"target_type": "Client", "target_id": str(uuid.uuid4())},
        {"target_type": "client; drop", "target_id": str(uuid.uuid4())},
        {"target_type": "", "target_id": str(uuid.uuid4())},
        {"target_type": "client", "target_id": str(uuid.uuid4()), "limit": 0},
        {"target_type": "client", "target_id": str(uuid.uuid4()), "limit": 101},
        {"target_type": "client", "target_id": str(uuid.uuid4()), "cursor": "garbage"},
        {"target_type": "client", "target_id": str(uuid.uuid4()), "actor_user_id": "x"},
        {"target_type": "client", "target_id": str(uuid.uuid4()), "from": "yesterday"},
        {"target_type": "client", "target_id": str(uuid.uuid4()), "q": ""},
        {"target_type": "client", "target_id": str(uuid.uuid4()), "include_related": "maybe"},
    ],
)
async def test_the_history_refuses_bad_parameters(client, params):
    owner, org = await practice(client)
    response = await client.get(audit_url(org, "/history"), headers=owner.headers, params=params)
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "VALIDATION_ERROR"


async def test_the_history_needs_a_signed_in_member_of_the_practice(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, "Acme Ltd")
    params = {"target_type": "client", "target_id": acme["id"]}
    anonymous = await client.get(audit_url(org, "/history"), params=params)
    assert anonymous.status_code == 401
    stranger = await create_user()
    assert (await history(client, stranger, org, "client", acme["id"])).status_code == 404
    # Someone from another practice reads nothing of this one, and nothing of theirs is mixed in.
    other_owner, other = await practice(client, "Other Practice")
    assert (await history(client, other_owner, org, "client", acme["id"])).status_code == 404
    empty = await history(client, other_owner, other, "client", acme["id"])
    assert empty.status_code == 200 and empty.json()["data"] == []


async def test_a_suspended_member_cannot_read_a_history(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, "Acme Ltd")
    gone = await add_member(org["id"], "viewer", status="suspended")
    assert (await history(client, gone, org, "client", acme["id"])).status_code == 404


# --- Cost figures -----------------------------------------------------------------------------


async def test_cost_figures_are_hidden_from_people_without_rate_read(client):
    owner, org = await practice(client)
    person = await staff(org, full_name="Ada Junior")
    posted = await client.post(
        org_url(org, f"/members/{person.id}/rates"),
        headers=owner.headers,
        json={"effective_from": "2026-01-01", "charge_rate": 10000, "cost_rate": 3500},
    )
    assert posted.status_code == 201, posted.text
    other = await staff(org, full_name="Sam Senior")
    cost = await new_rate(
        client, owner, org, kind="cost", user_id=other.id, rate=4000, effective_from="2025-01-01"
    )
    changed = await client.patch(
        org_url(org, f"/rates/{cost['id']}"), headers=owner.headers, json={"rate": 4500}
    )
    assert changed.status_code == 200, changed.text

    auditor = await reader(org)  # audit_log.read without rate.read
    for caller, sees in ((owner, True), (auditor, False)):
        [created] = (await listing(client, caller, org, target_type="staff_rate")).json()["data"]
        assert created["metadata"]["charge_rate"] == 10000
        assert created["metadata"]["cost_rate"] == (3500 if sees else None)

        [added] = (
            await listing(client, caller, org, action="rate.created", target_id=cost["id"])
        ).json()["data"]
        assert added["metadata"]["kind"] == "cost"
        assert added["metadata"]["rate"] == (4000 if sees else None)

        [updated] = (
            await listing(client, caller, org, action="rate.updated", target_id=cost["id"])
        ).json()["data"]
        # The rule after the change; the change itself is hidden with it.
        assert updated["metadata"]["rate"] == (4500 if sees else None)
        assert updated["metadata"]["changes"] == (
            {"rate": [4000, 4500]} if sees else {"rate": None}
        )
        expected = [{"field": "rate", "before": 4000, "after": 4500}] if sees else []
        assert updated["changes"] == expected

        shown = (await history(client, caller, org, "rate_rule", cost["id"])).json()["data"]
        assert [entry["action"] for entry in shown] == ["rate.updated", "rate.created"]
        assert shown[0]["changes"] == expected

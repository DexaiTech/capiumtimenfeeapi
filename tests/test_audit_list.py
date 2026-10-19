"""The audit-log list: who may read it, the additive fields, the filters and the text search."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.audit_helpers import audit_url, insert_entry, listing
from tests.clients_helpers import create_client, org_url
from tests.domain_factories import practice, staff
from tests.factories import create_user, error
from tests.foundation_helpers import add_custom_role

BASE = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


# The practice's own entries (its creation) are dated today: the window leaves them out.
WINDOW = {"from": at(-1).isoformat(), "to": at(60).isoformat()}


def actions(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [entry["action"] for entry in response.json()["data"]]


async def test_an_entry_carries_its_label_summary_and_normalised_changes(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, "Acme Ltd")
    await client.patch(
        org_url(org, f"/clients/{acme['id']}"), headers=owner.headers, json={"name": "Acme Group"}
    )

    response = await listing(client, owner, org, target_type="client")
    assert response.status_code == 200, response.text
    updated, created = response.json()["data"]
    assert set(updated) == {
        "id",
        "created_at",
        "action",
        "label",
        "summary",
        "actor",
        "target_type",
        "target_id",
        "ip_address",
        "user_agent",
        "changes",
        "metadata",
    }
    assert updated["action"] == "client.updated"
    assert updated["label"] == "Client details updated"
    assert updated["summary"] == f"Client details updated: {acme['code']} · Acme Group"
    assert updated["actor"] == {
        "id": str(owner.id),
        "full_name": owner.full_name,
        "email": owner.email,
    }
    assert updated["changes"] == [{"field": "name", "before": "Acme Ltd", "after": "Acme Group"}]
    # What the service recorded is still there, untouched.
    assert updated["metadata"] == {
        "code": acme["code"],
        "name": "Acme Group",
        "changes": {"name": ["Acme Ltd", "Acme Group"]},
    }
    assert updated["ip_address"] == "127.0.0.1"
    assert updated["user_agent"].startswith("python-httpx")
    assert created["action"] == "client.created" and created["changes"] == []


@pytest.mark.parametrize(
    ("role", "status"),
    [
        ("owner", 200),
        ("admin", 200),
        ("partner", 200),
        ("manager", 403),
        ("staff", 403),
        ("billing", 403),
        ("viewer", 403),
    ],
)
async def test_only_the_audit_log_permission_reads_the_list(client, role, status):
    owner, org = await practice(client)
    caller = owner if role == "owner" else await staff(org, role)
    assert (await listing(client, caller, org)).status_code == status


async def test_a_custom_role_reads_the_list_with_audit_log_read_only(client):
    _, org = await practice(client)
    await add_custom_role(org["id"], "Auditor", ["audit_log.read"])
    await add_custom_role(org["id"], "Exporter", ["audit_log.export"])
    assert (await listing(client, await staff(org, "Auditor"), org)).status_code == 200
    assert (await listing(client, await staff(org, "Exporter"), org)).status_code == 403


async def test_the_list_needs_a_signed_in_member_and_keeps_practices_apart(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other Practice")
    await insert_entry(org, "test.mine", actor=owner, meta={"note": "secret of the first"})
    await insert_entry(
        other, "test.theirs", actor=other_owner, meta={"note": "secret of the second"}
    )

    assert (await client.get(audit_url(org))).status_code == 401
    assert (await listing(client, await create_user(), org)).status_code == 404
    assert (await listing(client, other_owner, org)).status_code == 404
    mine = await listing(client, owner, org, q="secret")
    theirs = await listing(client, other_owner, other, q="secret")
    assert actions(mine) == ["test.mine"] and actions(theirs) == ["test.theirs"]
    # Another practice's record id finds nothing here, however it is asked for.
    record = uuid.uuid4()
    await insert_entry(other, "test.record", target_type="widget", target_id=record)
    assert actions(await listing(client, owner, org, target_id=str(record))) == []
    assert actions(await listing(client, other_owner, other, target_id=str(record))) == [
        "test.record"
    ]


async def test_the_filters_narrow_the_list(client):
    owner, org = await practice(client)
    other = await staff(org, "manager")
    first, second = uuid.uuid4(), uuid.uuid4()
    await insert_entry(
        org, "invoice.issued", actor=owner, target_type="invoice", target_id=first, created_at=at(0)
    )
    await insert_entry(
        org,
        "invoice.voided",
        actor=other,
        target_type="invoice",
        target_id=first,
        created_at=at(10),
    )
    await insert_entry(
        org,
        "payment.recorded",
        actor=owner,
        target_type="payment",
        target_id=second,
        created_at=at(20),
    )
    await insert_entry(
        org, "job.created", actor=other, target_type="job", target_id=second, created_at=at(30)
    )

    async def ask(**params):
        return actions(await listing(client, owner, org, **{**WINDOW, **params}))

    assert await ask() == ["job.created", "payment.recorded", "invoice.voided", "invoice.issued"]
    # An action matches exactly, or as a prefix when it ends with a dot.
    assert await ask(action="invoice.issued") == ["invoice.issued"]
    assert await ask(action="invoice") == []
    assert await ask(action="invoice.") == ["invoice.voided", "invoice.issued"]
    assert await ask(actor_user_id=str(other.id)) == ["job.created", "invoice.voided"]
    assert await ask(target_type="invoice") == ["invoice.voided", "invoice.issued"]
    assert await ask(target_id=str(second)) == ["job.created", "payment.recorded"]
    assert await ask(target_type="job", target_id=str(second)) == ["job.created"]
    assert await ask(target_type="invoice", target_id=str(second)) == []
    # from and to are inclusive.
    assert await ask(**{"from": at(10).isoformat(), "to": at(20).isoformat()}) == [
        "payment.recorded",
        "invoice.voided",
    ]
    # A timestamp without an offset is read as UTC.
    assert await ask(**{"from": "2026-03-10T12:25:00"}) == ["job.created"]
    assert await ask(action="invoice.", actor_user_id=str(other.id), target_id=str(first)) == [
        "invoice.voided"
    ]


async def test_the_text_search(client):
    owner, org = await practice(client)
    other = await staff(org, "manager", full_name="Grace Hopper", email="grace@example.org")
    await insert_entry(
        org,
        "invoice.issued",
        actor=owner,
        target_type="invoice",
        target_id=uuid.uuid4(),
        created_at=at(0),
        meta={"number": "INV-00012", "client_name": "Babbage Ltd"},
    )
    await insert_entry(
        org,
        "payment.recorded",
        actor=other,
        target_type="payment",
        target_id=uuid.uuid4(),
        created_at=at(10),
        ip="198.51.100.9",
        meta={"client_name": "Lovelace & Sons", "note": "100% paid"},
    )
    await insert_entry(
        org,
        "job.created",
        actor=other,
        target_type="job",
        target_id=uuid.uuid4(),
        created_at=at(20),
        meta={"note": "1000 hours"},
    )
    _, theirs = await practice(client, "Other Practice")
    await insert_entry(theirs, "invoice.issued", meta={"client_name": "Babbage Ltd"})

    async def ask(q: str):
        return actions(await listing(client, owner, org, q=q, **WINDOW))

    assert await ask("Babbage") == ["invoice.issued"]  # what the details say, in this practice
    assert await ask("  babbage ") == ["invoice.issued"]  # trimmed, case does not matter
    assert await ask("INV-00012") == ["invoice.issued"]
    assert await ask("payment") == ["payment.recorded"]  # the action
    assert await ask("job") == ["job.created"]
    assert await ask("198.51.100") == ["payment.recorded"]  # the address
    assert await ask("grace") == ["job.created", "payment.recorded"]  # the person's name
    assert await ask("HOPPER") == ["job.created", "payment.recorded"]
    assert await ask("grace@example.org") == ["job.created", "payment.recorded"]  # the email
    assert await ask("lovelace &") == ["payment.recorded"]
    # Wildcards are text: "%" finds the percent sign and nothing else.
    assert await ask("100%") == ["payment.recorded"]
    assert await ask("%") == ["payment.recorded"]
    assert await ask("100_") == []
    assert await ask("zzz") == []
    # It combines with the other filters.
    both = await listing(client, owner, org, q="grace", action="job.", **WINDOW)
    assert actions(both) == ["job.created"]


async def test_the_list_is_paginated_newest_first(client):
    owner, org = await practice(client)
    for index in range(30):
        await insert_entry(
            org,
            "test.entry",
            target_type="widget",
            target_id=uuid.uuid4(),
            created_at=at(index),
            meta={"n": index},
        )
    first = await listing(client, owner, org, target_type="widget")
    body = first.json()
    assert len(body["data"]) == 25 and body["meta"]["has_more"] is True  # the default page
    assert [e["metadata"]["n"] for e in body["data"]][:3] == [29, 28, 27]
    second = await listing(
        client, owner, org, target_type="widget", cursor=body["meta"]["next_cursor"]
    )
    assert [e["metadata"]["n"] for e in second.json()["data"]] == [4, 3, 2, 1, 0]
    assert second.json()["meta"] == {"next_cursor": None, "has_more": False}
    small = await listing(client, owner, org, target_type="widget", limit=10)
    assert len(small.json()["data"]) == 10


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"cursor": "garbage"},
        {"actor_user_id": "x"},
        {"target_id": "x"},
        {"target_type": "Not Valid"},
        {"target_type": "x" * 61},
        {"from": "yesterday"},
        {"to": "tomorrow"},
        {"q": ""},
        {"q": "x" * 201},
        {"action": ""},
        {"action": "x" * 101},
    ],
)
async def test_the_list_refuses_bad_parameters(client, params):
    owner, org = await practice(client)
    response = await client.get(audit_url(org), headers=owner.headers, params=params)
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "VALIDATION_ERROR"

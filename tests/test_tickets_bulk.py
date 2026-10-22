"""Bulk actions on tickets: status, priority and tags; limits, atomicity, permissions, one audit
row per changed ticket."""

import uuid

import pytest

from tests.clients_helpers import sql
from tests.factories import audit_entries, error
from tests.ticket_helpers import insert_ticket, make_ticket, new_team, tag_id, ticket_url, utc
from tests.time_helpers import ok_data


def bulk_url(t) -> str:
    return ticket_url(t.org, tail="/bulk")


async def bulk(client, t, actor, ids, action, value=None, **extra):
    body = {"ticket_ids": list(ids), "action": action, **extra}
    if value is not None:
        body["value"] = value
    return await client.post(bulk_url(t), headers=actor.headers, json=body)


async def three(client, t):
    """An open, a hold and a closed ticket."""
    a = await make_ticket(client, t.owner, t.org, "A", priority="high")
    b = await make_ticket(client, t.owner, t.org, "B", status="on_hold")
    c = await make_ticket(client, t.owner, t.org, "C", status="closed", priority="low")
    return a, b, c


async def current(client, t, ticket_id):
    return ok_data(await client.get(ticket_url(t.org, ticket_id), headers=t.owner.headers))


# --- Status ---------------------------------------------------------------------------------


async def test_set_status_moves_only_the_tickets_that_differ(client):
    t = await new_team(client)
    a, b, c = await three(client, t)
    closed_before = (await current(client, t, c["id"]))["closed_at"]
    response = await bulk(client, t, t.owner, [a["id"], b["id"], c["id"]], "set_status", "closed")
    data = ok_data(response)
    assert data == {"action": "set_status", "affected": 3, "changed": 2}
    assert response.json()["message"] == "2 tickets updated"

    after = {i: await current(client, t, i) for i in (a["id"], b["id"], c["id"])}
    assert all(row["status"] == "closed" for row in after.values())
    assert after[a["id"]]["closed_at"] is not None and after[b["id"]]["closed_at"] is not None
    assert after[c["id"]]["closed_at"] == closed_before  # untouched

    history = await sql(
        "SELECT ticket_id, from_status, to_status FROM ticket_status_history "
        "WHERE from_status IS NOT NULL"
    )
    assert {(str(h["ticket_id"]), h["from_status"], h["to_status"]) for h in history} == {
        (a["id"], "open", "closed"),
        (b["id"], "on_hold", "closed"),
    }
    entries = await audit_entries(t.id, "ticket.status_changed")
    assert {e["target_id"] for e in entries} == {a["id"], b["id"]}
    assert all(e["metadata"]["bulk"] is True and e["metadata"]["to"] == "closed" for e in entries)
    assert all(e["actor_user_id"] == str(t.owner.id) for e in entries)
    requests = await sql(
        "SELECT DISTINCT request_id FROM audit_logs WHERE action = 'ticket.status_changed'"
    )
    assert len(requests) == 1 and requests[0]["request_id"]


async def test_set_status_away_from_closed_clears_closed_at(client):
    t = await new_team(client)
    *_, c = await three(client, t)
    data = ok_data(await bulk(client, t, t.owner, [c["id"]], "set_status", "reopened"))
    assert data == {"action": "set_status", "affected": 1, "changed": 1}
    after = await current(client, t, c["id"])
    assert after["status"] == "reopened" and after["closed_at"] is None
    assert (await bulk(client, t, t.owner, [c["id"]], "set_status", "reopened")).json()[
        "message"
    ] == "0 tickets updated"


async def test_a_bulk_that_changes_nothing_writes_nothing(client):
    t = await new_team(client)
    a, *_ = await three(client, t)
    before = len(await audit_entries(t.id))
    data = ok_data(await bulk(client, t, t.owner, [a["id"]], "set_status", "open"))
    assert data["changed"] == 0 and data["affected"] == 1
    assert len(await audit_entries(t.id)) == before
    assert len(await sql("SELECT 1 FROM ticket_status_history")) == 3


# --- Priority -------------------------------------------------------------------------------


async def test_set_priority(client):
    t = await new_team(client)
    a, b, c = await three(client, t)
    data = ok_data(
        await bulk(client, t, t.owner, [a["id"], b["id"], c["id"]], "set_priority", "high")
    )
    assert data == {"action": "set_priority", "affected": 3, "changed": 2}  # a was high already
    assert [(await current(client, t, x["id"]))["priority"] for x in (a, b, c)] == ["high"] * 3
    entries = await audit_entries(t.id, "ticket.updated")
    assert {e["target_id"]: e["metadata"]["changes"] for e in entries} == {
        b["id"]: {"priority": ["not_set", "high"]},
        c["id"]: {"priority": ["low", "high"]},
    }
    assert all(e["metadata"]["bulk"] is True for e in entries)
    assert await audit_entries(t.id, "ticket.status_changed") == []


# --- Tags -----------------------------------------------------------------------------------


async def test_add_tag_is_idempotent_and_counts_what_changed(client):
    t = await new_team(client)
    bug, ui = await tag_id(t.org, "Bug"), await tag_id(t.org, "UI")
    a = await make_ticket(client, t.owner, t.org, "A", tag_ids=[bug])
    b = await make_ticket(client, t.owner, t.org, "B", tag_ids=[ui])
    c = await make_ticket(client, t.owner, t.org, "C")
    data = ok_data(await bulk(client, t, t.owner, [a["id"], b["id"], c["id"]], "add_tag", bug))
    assert data == {"action": "add_tag", "affected": 3, "changed": 2}
    names = {
        x["id"]: [tag["name"] for tag in (await current(client, t, x["id"]))["tags"]]
        for x in (a, b, c)
    }
    assert names == {a["id"]: ["Bug"], b["id"]: ["Bug", "UI"], c["id"]: ["Bug"]}
    assert len(await sql("SELECT 1 FROM ticket_tag_links")) == 4
    entries = await audit_entries(t.id, "ticket.updated")
    assert {e["target_id"]: e["metadata"]["changes"]["tags"] for e in entries} == {
        b["id"]: [["UI"], ["Bug", "UI"]],
        c["id"]: [[], ["Bug"]],
    }
    again = ok_data(await bulk(client, t, t.owner, [a["id"], b["id"], c["id"]], "add_tag", bug))
    assert again["changed"] == 0


async def test_remove_tag_touches_only_the_tickets_that_have_it(client):
    t = await new_team(client)
    bug, ui = await tag_id(t.org, "Bug"), await tag_id(t.org, "UI")
    a = await make_ticket(client, t.owner, t.org, "A", tag_ids=[bug, ui])
    b = await make_ticket(client, t.owner, t.org, "B", tag_ids=[ui])
    data = ok_data(await bulk(client, t, t.owner, [a["id"], b["id"]], "remove_tag", bug))
    assert data == {"action": "remove_tag", "affected": 2, "changed": 1}
    assert [tag["name"] for tag in (await current(client, t, a["id"]))["tags"]] == ["UI"]
    assert [tag["name"] for tag in (await current(client, t, b["id"]))["tags"]] == ["UI"]
    [entry] = await audit_entries(t.id, "ticket.updated")
    assert entry["target_id"] == a["id"]
    assert entry["metadata"]["changes"]["tags"] == [["Bug", "UI"], ["UI"]]


async def test_tags_are_checked_against_the_practice(client):
    t = await new_team(client)
    other = await new_team(client, "Other Practice")
    a = await make_ticket(client, t.owner, t.org)
    for action in ("add_tag", "remove_tag"):
        for value in (await tag_id(other.org, "Bug"), str(uuid.uuid4())):
            response = await bulk(client, t, t.owner, [a["id"]], action, value)
            assert response.status_code == 422, (action, value)
            assert error(response)["details"][0]["field"] == "value"


# --- Limits and atomicity -------------------------------------------------------------------


async def test_the_selection_is_limited_to_200_and_may_not_be_empty(client):
    t = await new_team(client)
    for ids in ([], [str(uuid.uuid4()) for _ in range(201)]):
        response = await bulk(client, t, t.owner, ids, "set_priority", "low")
        assert response.status_code == 422
        assert error(response)["details"][0]["field"] == "ticket_ids"
    # 200 passes validation (the tickets just do not exist).
    unknown = await bulk(
        client, t, t.owner, [str(uuid.uuid4()) for _ in range(200)], "set_priority", "low"
    )
    assert unknown.status_code == 404


async def test_a_selection_may_be_200_real_tickets(client):
    t = await new_team(client)
    rows = []
    for number in range(1, 201):
        rows.append(await insert_ticket(t.org, t.owner, created_at=utc(2026, 9, 1), number=number))
    data = ok_data(await bulk(client, t, t.owner, rows, "set_status", "closed"))
    assert data == {"action": "set_status", "affected": 200, "changed": 200}
    assert len(await audit_entries(t.id, "ticket.status_changed")) == 200
    assert await sql("SELECT count(*) AS n FROM tickets WHERE status = 'closed'") == [{"n": 200}]


async def test_repeated_ids_count_once(client):
    t = await new_team(client)
    a, b, _ = await three(client, t)
    data = ok_data(
        await bulk(
            client, t, t.owner, [a["id"], a["id"], b["id"], a["id"]], "set_priority", "medium"
        )
    )
    assert data == {"action": "set_priority", "affected": 2, "changed": 2}


async def test_one_foreign_id_refuses_the_whole_batch(client):
    t = await new_team(client)
    other = await new_team(client, "Other Practice")
    a, b, _ = await three(client, t)
    foreign = await make_ticket(client, other.owner, other.org, "Foreign")
    missing = str(uuid.uuid4())
    before = len(await audit_entries(t.id))
    response = await bulk(
        client, t, t.owner, [a["id"], foreign["id"], b["id"], missing], "set_status", "closed"
    )
    assert response.status_code == 404
    assert error(response)["code"] == "RESOURCE_NOT_FOUND"
    assert sorted(error(response)["details"]["ticket_ids"]) == sorted([foreign["id"], missing])
    assert (await current(client, t, a["id"]))["status"] == "open"
    assert (await current(client, t, b["id"]))["status"] == "on_hold"
    assert len(await sql("SELECT 1 FROM ticket_status_history")) == 4  # 3 + the foreign one
    assert len(await audit_entries(t.id)) == before
    untouched = ok_data(
        await client.get(ticket_url(other.org, foreign["id"]), headers=other.owner.headers)
    )
    assert untouched["status"] == "open"


# --- Validation -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"action": "set_status"}, "value"),
        ({"action": "set_status", "value": "done"}, "value"),
        ({"action": "set_status", "value": "OPEN"}, "value"),
        ({"action": "set_priority"}, "value"),
        ({"action": "set_priority", "value": "urgent"}, "value"),
        ({"action": "add_tag"}, "value"),
        ({"action": "add_tag", "value": "not-a-uuid"}, "value"),
        ({"action": "remove_tag", "value": "123"}, "value"),
        ({"action": "archive"}, "action"),
        ({"action": "set_status", "value": "open", "extra": 1}, "extra"),
        ({"value": "open"}, "action"),
    ],
)
async def test_bulk_validation(client, body, field):
    t = await new_team(client)
    a = await make_ticket(client, t.owner, t.org)
    response = await client.post(
        bulk_url(t), headers=t.owner.headers, json={"ticket_ids": [a["id"]], **body}
    )
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == field
    assert (await current(client, t, a["id"]))["status"] == "open"


async def test_ticket_ids_must_be_a_list_of_ids(client):
    t = await new_team(client)
    for ids in ("x", [1], ["nope"], None):
        response = await client.post(
            bulk_url(t),
            headers=t.owner.headers,
            json={"ticket_ids": ids, "action": "set_priority", "value": "low"},
        )
        assert response.status_code == 422, ids


# --- Permissions ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "can_update"),
    [
        ("owner", True),
        ("admin", True),
        ("partner", True),
        ("manager", True),
        ("staff", True),
        ("billing", True),
        ("viewer", False),
    ],
)
async def test_bulk_permissions_per_role(client, role, can_update):
    t = await new_team(client)
    actor = await t.member(role)
    a, *_ = await three(client, t)
    update = await bulk(client, t, actor, [a["id"]], "set_priority", "medium")
    assert update.status_code == (200 if can_update else 403)
    tagged = await bulk(client, t, actor, [a["id"]], "add_tag", await tag_id(t.org, "Bug"))
    assert tagged.status_code == (200 if can_update else 403)

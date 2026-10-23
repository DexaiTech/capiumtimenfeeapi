"""Tickets: creating, reading, changing and deleting them; validation; permissions per role;
tenancy; the status history; audit rows; the gapless ticket numbers."""

import asyncio
import uuid

import pytest

from app.services.ticket_reader import TicketReader
from tests.clients_helpers import sql
from tests.factories import audit_entries, create_user, error
from tests.ticket_helpers import (
    DETAIL_KEYS,
    insert_ticket,
    make_ticket,
    new_team,
    tag_id,
    ticket_url,
    utc,
)
from tests.time_helpers import ok_data

# --- Create ---------------------------------------------------------------------------------


async def test_create_a_minimal_ticket(client):
    t = await new_team(client)
    response = await client.post(
        ticket_url(t.org), headers=t.owner.headers, json={"subject": "  Login   page crashes "}
    )
    data = ok_data(response, 201)
    assert set(data) == DETAIL_KEYS
    assert (data["code"], data["number"], data["category"]) == ("APT1", 1, "application")
    assert data["subject"] == "Login page crashes"
    assert (data["status"], data["priority"]) == ("open", "not_set")
    assert data["description"] is None
    assert data["tags"] == [] and data["assignee"] is None and data["attachments"] == []
    assert data["created_by"] == str(t.owner.id)
    assert data["created_by_name"] == t.owner.full_name
    assert data["closed_at"] is None
    assert (data["comment_count"], data["attachment_count"]) == (0, 0)
    assert response.json()["message"] == "Ticket created"


async def test_create_a_full_ticket(client):
    t = await new_team(client)
    staff = await t.member("staff")
    bug, reporting = await tag_id(t.org, "Bug"), await tag_id(t.org, "Reporting")
    data = await make_ticket(
        client,
        t.owner,
        t.org,
        "Trial balance does not agree",
        description="Line one\r\nLine two",
        status="on_hold",
        priority="high",
        tag_ids=[reporting, bug, bug],
        assignee_id=str(staff.id),
    )
    assert (data["status"], data["priority"]) == ("on_hold", "high")
    assert data["description"] == "Line one\nLine two"
    assert [tag["name"] for tag in data["tags"]] == ["Bug", "Reporting"]
    assert data["tags"][0] == {"id": bug, "name": "Bug", "color": "red"}
    assert data["assignee"] == {"id": str(staff.id), "full_name": staff.full_name}


async def test_a_ticket_created_closed_has_closed_at(client):
    t = await new_team(client)
    data = await make_ticket(client, t.owner, t.org, status="closed")
    assert data["status"] == "closed" and data["closed_at"] is not None
    rows = await sql("SELECT from_status, to_status, changed_at FROM ticket_status_history")
    assert [(r["from_status"], r["to_status"]) for r in rows] == [(None, "closed")]


async def test_a_new_ticket_is_created_and_updated_at_the_same_moment(client):
    t = await new_team(client)
    data = await make_ticket(client, t.owner, t.org, status="closed")
    assert data["created_at"] == data["updated_at"] == data["closed_at"]
    [row] = await sql("SELECT changed_at FROM ticket_status_history")
    assert row["changed_at"].isoformat().replace("+00:00", "Z") == data["created_at"]
    later = ok_data(
        await client.patch(
            ticket_url(t.org, data["id"]), headers=t.owner.headers, json={"priority": "low"}
        )
    )
    assert later["created_at"] == data["created_at"] and later["updated_at"] > data["updated_at"]


async def test_create_writes_the_first_status_history_row_and_an_audit_row(client):
    t = await new_team(client)
    bug = await tag_id(t.org, "Bug")
    data = await make_ticket(client, t.owner, t.org, tag_ids=[bug], priority="low")
    history = await sql(
        "SELECT from_status, to_status, changed_by, changed_at FROM ticket_status_history "
        "WHERE ticket_id = :t",
        t=data["id"],
    )
    assert len(history) == 1
    assert (history[0]["from_status"], history[0]["to_status"]) == (None, "open")
    assert str(history[0]["changed_by"]) == str(t.owner.id)
    created = await sql("SELECT created_at FROM tickets WHERE id = :t", t=data["id"])
    assert history[0]["changed_at"] == created[0]["created_at"]

    [entry] = await audit_entries(t.id, "ticket.created")
    assert entry["actor_user_id"] == str(t.owner.id)
    assert (entry["target_type"], entry["target_id"]) == ("ticket", data["id"])
    assert entry["metadata"] == {
        "code": "APT1",
        "subject": "Login page crashes",
        "status": "open",
        "priority": "low",
        "assignee_id": None,
        "tags": ["Bug"],
    }
    [row] = await sql(
        "SELECT ip_address::text AS ip, request_id FROM audit_logs WHERE action = 'ticket.created'"
    )
    assert row["ip"] and row["request_id"]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "subject"),
        ({"subject": ""}, "subject"),
        ({"subject": "   "}, "subject"),
        ({"subject": "x" * 201}, "subject"),
        ({"subject": "bad\u0000name"}, "subject"),
        ({"subject": 5}, "subject"),
        ({"subject": "ok", "description": "x" * 10_001}, "description"),
        ({"subject": "ok", "description": "nul\u0000"}, "description"),
        ({"subject": "ok", "status": "done"}, "status"),
        ({"subject": "ok", "status": None}, "status"),
        ({"subject": "ok", "priority": "urgent"}, "priority"),
        ({"subject": "ok", "tag_ids": ["not-a-uuid"]}, "tag_ids.0"),
        ({"subject": "ok", "tag_ids": [str(uuid.uuid4())] * 21}, "tag_ids"),
        ({"subject": "ok", "assignee_id": "nobody"}, "assignee_id"),
        ({"subject": "ok", "category": "hardware"}, "category"),
        ({"subject": "ok", "unknown": 1}, "unknown"),
    ],
)
async def test_create_validation(client, body, field):
    t = await new_team(client)
    response = await client.post(ticket_url(t.org), headers=t.owner.headers, json=body)
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "VALIDATION_ERROR"
    assert error(response)["details"][0]["field"] == field
    assert await sql("SELECT 1 FROM tickets") == []


async def test_create_refuses_foreign_unknown_and_inactive_references(client):
    t = await new_team(client)
    other = await new_team(client, "Other Practice")
    outsider = await create_user()
    staff = await t.member("staff")
    cases = [
        ("tag_ids", [await tag_id(other.org, "Bug")]),
        ("tag_ids", [str(uuid.uuid4())]),
        ("assignee_id", str(other.owner.id)),
        ("assignee_id", str(outsider.id)),
        ("assignee_id", str(uuid.uuid4())),
    ]
    for field, value in cases:
        response = await client.post(
            ticket_url(t.org), headers=t.owner.headers, json={"subject": "Refused", field: value}
        )
        assert response.status_code == 422, (field, value)
        assert error(response)["details"][0]["field"] == field
    for status in ("suspended", "removed", "inactive", "left"):
        await sql(
            "UPDATE organization_members SET status = :s WHERE organization_id = :o "
            "AND user_id = :u",
            s=status,
            o=t.id,
            u=staff.id,
        )
        response = await client.post(
            ticket_url(t.org),
            headers=t.owner.headers,
            json={"subject": "Refused", "assignee_id": str(staff.id)},
        )
        assert response.status_code == 422, status
    assert await sql("SELECT 1 FROM tickets") == []


# --- Get ------------------------------------------------------------------------------------


async def test_get_a_ticket(client):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org, description="Details")
    data = ok_data(await client.get(ticket_url(t.org, ticket["id"]), headers=t.owner.headers))
    assert set(data) == DETAIL_KEYS
    assert data == ticket


async def test_get_unknown_and_malformed_ids(client):
    t = await new_team(client)
    unknown = await client.get(ticket_url(t.org, uuid.uuid4()), headers=t.owner.headers)
    assert unknown.status_code == 404 and error(unknown)["code"] == "RESOURCE_NOT_FOUND"
    malformed = await client.get(ticket_url(t.org, "nope"), headers=t.owner.headers)
    assert malformed.status_code == 422


# --- Update ---------------------------------------------------------------------------------


async def test_patch_changes_the_fields_and_audits_them(client):
    t = await new_team(client)
    staff = await t.member("staff")
    bug, reporting, accounts = [await tag_id(t.org, n) for n in ("Bug", "Reporting", "Accounts")]
    ticket = await make_ticket(client, t.owner, t.org, description="Old text", tag_ids=[bug])
    url = ticket_url(t.org, ticket["id"])
    response = await client.patch(
        url,
        headers=staff.headers,
        json={
            "priority": "high",
            "assignee_id": str(staff.id),
            "tag_ids": [reporting, accounts],
        },
    )
    data = ok_data(response)
    assert response.json()["message"] == "Ticket updated"
    assert (data["subject"], data["description"], data["priority"]) == (
        "Login page crashes",
        "Old text",
        "high",
    )
    assert data["assignee"]["id"] == str(staff.id)
    assert [tag["name"] for tag in data["tags"]] == ["Accounts", "Reporting"]
    assert data["updated_at"] >= ticket["updated_at"]

    [entry] = await audit_entries(t.id, "ticket.updated")
    assert entry["actor_user_id"] == str(staff.id)
    assert entry["metadata"]["code"] == "APT1"
    assert entry["metadata"]["changes"] == {
        "priority": ["not_set", "high"],
        "assignee_id": [None, str(staff.id)],
        "tags": [["Bug"], ["Accounts", "Reporting"]],
    }
    assert await audit_entries(t.id, "ticket.status_changed") == []


async def test_patch_status_writes_history_and_closed_at(client):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org)
    url = ticket_url(t.org, ticket["id"])
    closed = ok_data(await client.patch(url, headers=t.owner.headers, json={"status": "closed"}))
    assert closed["status"] == "closed" and closed["closed_at"] is not None
    reopened = ok_data(
        await client.patch(url, headers=t.owner.headers, json={"status": "reopened"})
    )
    assert reopened["status"] == "reopened" and reopened["closed_at"] is None
    ok_data(await client.patch(url, headers=t.owner.headers, json={"status": "reopened"}))  # no-op

    history = await sql(
        "SELECT from_status, to_status, changed_by FROM ticket_status_history "
        "WHERE ticket_id = :t ORDER BY changed_at, id",
        t=ticket["id"],
    )
    assert [(h["from_status"], h["to_status"]) for h in history] == [
        (None, "open"),
        ("open", "closed"),
        ("closed", "reopened"),
    ]
    assert {str(h["changed_by"]) for h in history} == {str(t.owner.id)}

    changed = await audit_entries(t.id, "ticket.status_changed")
    assert [(e["metadata"]["from"], e["metadata"]["to"]) for e in changed] == [
        ("open", "closed"),
        ("closed", "reopened"),
    ]
    assert changed[0]["metadata"]["changes"] == {"status": ["open", "closed"]}
    assert await audit_entries(t.id, "ticket.updated") == []


async def test_patch_status_and_other_fields_write_both_audit_rows(client):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org)
    response = await client.patch(
        ticket_url(t.org, ticket["id"]),
        headers=t.owner.headers,
        json={"status": "in_progress", "priority": "medium"},
    )
    assert response.status_code == 200
    assert len(await audit_entries(t.id, "ticket.updated")) == 1
    assert len(await audit_entries(t.id, "ticket.status_changed")) == 1


async def test_patch_null_clears_the_assignee_but_nothing_else(client):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await make_ticket(client, t.owner, t.org, assignee_id=str(staff.id))
    url = ticket_url(t.org, ticket["id"])
    for field in ("status", "priority", "tag_ids"):
        response = await client.patch(url, headers=t.owner.headers, json={field: None})
        assert response.status_code == 422, field
        assert error(response)["details"][0]["field"] == field
    cleared = ok_data(await client.patch(url, headers=t.owner.headers, json={"assignee_id": None}))
    assert cleared["assignee"] is None


async def test_the_subject_and_the_description_cannot_be_changed_after_creation(client):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org, description="Written once")
    url = ticket_url(t.org, ticket["id"])
    for body in (
        {"subject": "Another subject"},
        {"description": "Another text"},
        {"description": None},
        {"description_html": "<p>Another</p>"},
        {"subject": ticket["subject"]},
    ):
        response = await client.patch(url, headers=t.owner.headers, json=body)
        assert response.status_code == 422, body
    after = ok_data(await client.get(url, headers=t.owner.headers))
    assert (after["subject"], after["description"]) == (ticket["subject"], "Written once")
    assert await audit_entries(t.id, "ticket.updated") == []


async def test_patch_tags_replace_and_clear(client):
    t = await new_team(client)
    bug, ui = await tag_id(t.org, "Bug"), await tag_id(t.org, "UI")
    ticket = await make_ticket(client, t.owner, t.org, tag_ids=[bug])
    url = ticket_url(t.org, ticket["id"])
    swapped = ok_data(await client.patch(url, headers=t.owner.headers, json={"tag_ids": [ui]}))
    assert [tag["name"] for tag in swapped["tags"]] == ["UI"]
    same = ok_data(await client.patch(url, headers=t.owner.headers, json={"tag_ids": [ui, ui]}))
    assert [tag["name"] for tag in same["tags"]] == ["UI"]
    cleared = ok_data(await client.patch(url, headers=t.owner.headers, json={"tag_ids": []}))
    assert cleared["tags"] == []
    assert len(await audit_entries(t.id, "ticket.updated")) == 2  # the repeat changed nothing


async def test_patch_that_changes_nothing_writes_nothing(client):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org, priority="low")
    url = ticket_url(t.org, ticket["id"])
    for body in ({}, {"priority": "low"}, {"status": "open"}):
        assert (await client.patch(url, headers=t.owner.headers, json=body)).status_code == 200
    assert await audit_entries(t.id, "ticket.updated") == []
    assert await audit_entries(t.id, "ticket.status_changed") == []
    assert len(await sql("SELECT 1 FROM ticket_status_history")) == 1


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"status": "done"}, "status"),
        ({"priority": "urgent"}, "priority"),
        ({"tag_ids": [str(uuid.uuid4())]}, "tag_ids"),
        ({"assignee_id": str(uuid.uuid4())}, "assignee_id"),
        ({"assignee_id": "nobody"}, "assignee_id"),
        ({"code": "APT9"}, "code"),
        ({"number": 9}, "number"),
    ],
)
async def test_patch_validation(client, body, field):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org)
    response = await client.patch(
        ticket_url(t.org, ticket["id"]), headers=t.owner.headers, json=body
    )
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == field


async def test_an_assignee_who_became_inactive_does_not_block_other_changes(client):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await make_ticket(client, t.owner, t.org, assignee_id=str(staff.id))
    await sql(
        "UPDATE organization_members SET status = 'left' WHERE organization_id = :o "
        "AND user_id = :u",
        o=t.id,
        u=staff.id,
    )
    url = ticket_url(t.org, ticket["id"])
    changed = ok_data(await client.patch(url, headers=t.owner.headers, json={"priority": "high"}))
    assert changed["assignee"]["id"] == str(staff.id)
    refused = await client.patch(
        ticket_url(t.org, (await make_ticket(client, t.owner, t.org))["id"]),
        headers=t.owner.headers,
        json={"assignee_id": str(staff.id)},
    )
    assert refused.status_code == 422


# --- Permissions ----------------------------------------------------------------------------

# role -> (list, get, create, update)
EXPECTED_BY_ROLE = {
    "owner": (200, 200, 201, 200),
    "admin": (200, 200, 201, 200),
    "partner": (200, 200, 201, 200),
    "manager": (200, 200, 201, 200),
    "staff": (200, 200, 201, 200),
    "billing": (200, 200, 201, 200),
    "viewer": (200, 200, 403, 403),
}


@pytest.mark.parametrize("role", list(EXPECTED_BY_ROLE))
async def test_ticket_permissions_per_system_role(client, role):
    t = await new_team(client)
    actor = await t.member(role)
    ticket = await make_ticket(client, t.owner, t.org)
    url = ticket_url(t.org, ticket["id"])
    got = (
        (await client.get(ticket_url(t.org), headers=actor.headers)).status_code,
        (await client.get(url, headers=actor.headers)).status_code,
        (
            await client.post(ticket_url(t.org), headers=actor.headers, json={"subject": "New"})
        ).status_code,
        (await client.patch(url, headers=actor.headers, json={"priority": "high"})).status_code,
    )
    assert got == EXPECTED_BY_ROLE[role]


async def test_a_custom_role_gets_exactly_the_permissions_it_is_given(client):
    t = await new_team(client)
    created = await client.post(
        t.url("/roles"),
        headers=t.owner.headers,
        json={
            "name": "Triager",
            "permissions": ["organization.read", "ticket.read", "ticket.update"],
        },
    )
    assert created.status_code == 201, created.text
    triager = await t.member("Triager")
    ticket = await make_ticket(client, t.owner, t.org)
    url = ticket_url(t.org, ticket["id"])
    assert (await client.get(url, headers=triager.headers)).status_code == 200
    assert (
        await client.patch(url, headers=triager.headers, json={"priority": "low"})
    ).status_code == 200
    create = await client.post(ticket_url(t.org), headers=triager.headers, json={"subject": "x"})
    assert create.status_code == 403


async def test_a_member_without_ticket_permissions_is_refused_everywhere(client):
    t = await new_team(client)
    created = await client.post(
        t.url("/roles"),
        headers=t.owner.headers,
        json={"name": "No tickets", "permissions": ["organization.read"]},
    )
    assert created.status_code == 201, created.text
    nobody = await t.member("No tickets")
    ticket = await make_ticket(client, t.owner, t.org)
    url = ticket_url(t.org, ticket["id"])
    calls = [
        client.get(ticket_url(t.org), headers=nobody.headers),
        client.get(url, headers=nobody.headers),
        client.get(ticket_url(t.org, tail="/dashboard"), headers=nobody.headers),
        client.post(ticket_url(t.org), headers=nobody.headers, json={"subject": "x"}),
        client.patch(url, headers=nobody.headers, json={"priority": "low"}),
        client.post(
            ticket_url(t.org, tail="/bulk"),
            headers=nobody.headers,
            json={"ticket_ids": [ticket["id"]], "action": "set_priority", "value": "low"},
        ),
    ]
    assert [r.status_code for r in await asyncio.gather(*calls)] == [403] * 6


# --- Tenancy --------------------------------------------------------------------------------


async def test_another_practices_tickets_are_not_found(client):
    mine = await new_team(client)
    theirs = await new_team(client, "Their Practice")
    foreign = await make_ticket(client, theirs.owner, theirs.org, "Their secret")
    foreign_url = ticket_url(theirs.org, foreign["id"])

    # Their ticket id on my practice's path: not mine.
    on_mine = ticket_url(mine.org, foreign["id"])
    assert (await client.get(on_mine, headers=mine.owner.headers)).status_code == 404
    assert (
        await client.patch(on_mine, headers=mine.owner.headers, json={"priority": "low"})
    ).status_code == 404

    # Their practice's path is a practice I do not belong to: also not found, never 403.
    assert (await client.get(foreign_url, headers=mine.owner.headers)).status_code == 404
    assert (await client.get(ticket_url(theirs.org), headers=mine.owner.headers)).status_code == 404
    created = await client.post(
        ticket_url(theirs.org), headers=mine.owner.headers, json={"subject": "Intrusion"}
    )
    assert created.status_code == 404
    assert (
        await client.get(ticket_url(theirs.org, tail="/dashboard"), headers=mine.owner.headers)
    ).status_code == 404

    # Nothing of theirs shows up in mine, and theirs is untouched.
    assert ok_data(await client.get(ticket_url(mine.org), headers=mine.owner.headers)) == []
    untouched = ok_data(await client.get(foreign_url, headers=theirs.owner.headers))
    assert untouched["subject"] == "Their secret" and untouched["priority"] == "not_set"


async def test_the_database_refuses_cross_practice_references(client):
    mine = await new_team(client)
    theirs = await new_team(client, "Their Practice")
    foreign_staff = await theirs.member("staff")
    with pytest.raises(Exception, match="fk_tickets_organization_id_assignee_members"):
        await insert_ticket(
            mine.org, mine.owner, created_at=utc(2026, 9, 1), assignee=foreign_staff
        )


async def test_deleting_a_practice_member_reference_is_protected(client):
    """A person who raised tickets cannot be deleted away from them."""
    t = await new_team(client)
    staff = await t.member("staff")
    await make_ticket(client, staff, t.org)
    with pytest.raises(Exception, match="violates foreign key constraint"):
        await sql(
            "DELETE FROM organization_members WHERE organization_id = :o AND user_id = :u",
            o=t.id,
            u=staff.id,
        )


# --- Numbering ------------------------------------------------------------------------------


async def test_numbers_run_one_two_three_per_practice(client):
    a = await new_team(client)
    b = await new_team(client, "Second Practice")
    codes_a = [(await make_ticket(client, a.owner, a.org))["code"] for _ in range(3)]
    codes_b = [(await make_ticket(client, b.owner, b.org))["code"] for _ in range(2)]
    assert codes_a == ["APT1", "APT2", "APT3"]
    assert codes_b == ["APT1", "APT2"]
    sequence = await sql(
        "SELECT prefix, padding, next_number FROM document_sequences "
        "WHERE organization_id = :o AND kind = 'ticket'",
        o=a.id,
    )
    assert sequence == [{"prefix": "APT", "padding": 1, "next_number": 4}]


async def test_failed_creates_leave_no_gap(client, monkeypatch):
    t = await new_team(client)
    assert (await make_ticket(client, t.owner, t.org))["code"] == "APT1"
    # Refused before a number is taken.
    refused = await client.post(
        ticket_url(t.org),
        headers=t.owner.headers,
        json={"subject": "x", "tag_ids": [str(uuid.uuid4())]},
    )
    assert refused.status_code == 422

    # Fails after the number was taken: the transaction rolls back and releases it.
    async def boom(self, organization_id, ticket_id):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(TicketReader, "detail", boom)
    broken = await client.post(ticket_url(t.org), headers=t.owner.headers, json={"subject": "x"})
    assert broken.status_code == 500
    monkeypatch.undo()
    assert await sql("SELECT count(*) AS n FROM tickets") == [{"n": 1}]
    assert (await make_ticket(client, t.owner, t.org))["code"] == "APT2"


async def test_concurrent_creates_get_each_number_exactly_once(client):
    t = await new_team(client)
    staff = await t.member("staff")
    posts = [
        client.post(
            ticket_url(t.org),
            headers=(t.owner if i % 2 else staff).headers,
            json={"subject": f"T{i}"},
        )
        for i in range(12)
    ]
    responses = await asyncio.gather(*posts)
    assert [r.status_code for r in responses] == [201] * 12
    assert sorted(r.json()["data"]["number"] for r in responses) == list(range(1, 13))
    assert len({r.json()["data"]["code"] for r in responses}) == 12
    # Tickets are numbered in the order they were created.
    rows = await sql(
        "SELECT number FROM tickets WHERE organization_id = :o ORDER BY created_at, number",
        o=t.id,
    )
    assert [r["number"] for r in rows] == list(range(1, 13))
    next_number = await sql("SELECT next_number FROM document_sequences WHERE kind = 'ticket'")
    assert next_number == [{"next_number": 13}]


async def test_a_changed_prefix_applies_to_new_tickets_only(client):
    t = await new_team(client)
    first = await make_ticket(client, t.owner, t.org)
    changed = await client.patch(
        t.url("/sequences/ticket"),
        headers=t.owner.headers,
        json={"prefix": "BUG", "next_number": 40},
    )
    assert changed.status_code == 200, changed.text
    second = await make_ticket(client, t.owner, t.org)
    assert (first["code"], second["code"]) == ("APT1", "BUG40")
    assert second["number"] == 40
    got = ok_data(await client.get(ticket_url(t.org, first["id"]), headers=t.owner.headers))
    assert got["code"] == "APT1"


async def test_new_practices_start_with_the_default_tags_and_the_ticket_sequence(client):
    t = await new_team(client)
    tags = await sql("SELECT name, color FROM ticket_tags WHERE organization_id = :o", o=t.id)
    assert {row["name"] for row in tags} == {
        "Bug",
        "Enhancement",
        "Accounts",
        "Reporting",
        "UI",
        "Trial Balance",
        "Validation",
    }
    assert {row["color"] for row in tags} <= {"violet", "blue", "green", "red", "orange", "grey"}
    sequences = await sql("SELECT kind FROM document_sequences WHERE organization_id = :o", o=t.id)
    assert "ticket" in {row["kind"] for row in sequences}

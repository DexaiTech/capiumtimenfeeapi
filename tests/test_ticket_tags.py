"""Ticket tags: the practice's labels."""

import asyncio
import uuid

import pytest

from tests.clients_helpers import sql
from tests.factories import audit_entries, error
from tests.ticket_helpers import make_ticket, new_team, tag_id, ticket_url
from tests.time_helpers import ok_data

TAG_KEYS = {"id", "name", "color", "ticket_count", "created_at", "updated_at"}
DEFAULT_NAMES = ["Accounts", "Bug", "Enhancement", "Reporting", "Trial Balance", "UI", "Validation"]


def tags_url(t, tag_id_=None) -> str:
    return t.url("/ticket-tags" + (f"/{tag_id_}" if tag_id_ else ""))


# --- List -----------------------------------------------------------------------------------


async def test_a_new_practice_lists_the_seven_default_tags_by_name(client):
    t = await new_team(client)
    response = await client.get(tags_url(t), headers=t.owner.headers)
    data = ok_data(response)
    assert [tag["name"] for tag in data] == DEFAULT_NAMES
    assert all(set(tag) == TAG_KEYS for tag in data)
    assert all(tag["ticket_count"] == 0 for tag in data)
    colours = {tag["name"]: tag["color"] for tag in data}
    assert colours["Bug"] == "red"
    assert set(colours.values()) <= {"violet", "blue", "green", "red", "orange", "grey"}
    assert response.json()["meta"] is None  # bounded: not paginated


async def test_the_list_counts_the_tickets_that_carry_each_tag(client):
    t = await new_team(client)
    bug, ui = await tag_id(t.org, "Bug"), await tag_id(t.org, "UI")
    await make_ticket(client, t.owner, t.org, "A", tag_ids=[bug, ui])
    await make_ticket(client, t.owner, t.org, "B", tag_ids=[bug])
    counts = {
        tag["name"]: tag["ticket_count"]
        for tag in ok_data(await client.get(tags_url(t), headers=t.owner.headers))
    }
    assert counts["Bug"] == 2 and counts["UI"] == 1 and counts["Accounts"] == 0


async def test_each_practice_sees_only_its_own_tags(client):
    mine = await new_team(client)
    theirs = await new_team(client, "Their Practice")
    created = await client.post(
        tags_url(theirs), headers=theirs.owner.headers, json={"name": "Theirs only"}
    )
    assert created.status_code == 201
    names = [
        tag["name"] for tag in ok_data(await client.get(tags_url(mine), headers=mine.owner.headers))
    ]
    assert "Theirs only" not in names and len(names) == 7
    # Their path is a practice I do not belong to.
    assert (await client.get(tags_url(theirs), headers=mine.owner.headers)).status_code == 404


# --- Create ---------------------------------------------------------------------------------


async def test_create_a_tag(client):
    t = await new_team(client)
    response = await client.post(
        tags_url(t), headers=t.owner.headers, json={"name": "  Needs   review ", "color": "orange"}
    )
    data = ok_data(response, 201)
    assert set(data) == TAG_KEYS
    assert (data["name"], data["color"], data["ticket_count"]) == ("Needs review", "orange", 0)
    assert response.json()["message"] == "Tag created"
    [entry] = await audit_entries(t.id, "ticket_tag.created")
    assert entry["target_id"] == data["id"] and entry["actor_user_id"] == str(t.owner.id)
    assert entry["metadata"] == {"name": "Needs review", "color": "orange"}
    listed = [
        tag["name"] for tag in ok_data(await client.get(tags_url(t), headers=t.owner.headers))
    ]
    assert "Needs review" in listed


async def test_the_colour_defaults_to_grey_and_names_may_be_unicode(client):
    t = await new_team(client)
    data = ok_data(
        await client.post(tags_url(t), headers=t.owner.headers, json={"name": "Zoë ✓"}), 201
    )
    assert data["color"] == "grey" and data["name"] == "Zoë ✓"


@pytest.mark.parametrize("colour", ["violet", "blue", "green", "red", "orange", "grey"])
async def test_every_documented_colour_is_accepted(client, colour):
    t = await new_team(client)
    data = ok_data(
        await client.post(
            tags_url(t), headers=t.owner.headers, json={"name": "Coloured", "color": colour}
        ),
        201,
    )
    assert data["color"] == colour


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "name"),
        ({"name": ""}, "name"),
        ({"name": "   "}, "name"),
        ({"name": "x" * 41}, "name"),
        ({"name": "nul\u0000"}, "name"),
        ({"name": 5}, "name"),
        ({"name": "Ok", "color": "pink"}, "color"),
        ({"name": "Ok", "color": None}, "color"),
        ({"name": "Ok", "extra": 1}, "extra"),
    ],
)
async def test_create_validation(client, body, field):
    t = await new_team(client)
    response = await client.post(tags_url(t), headers=t.owner.headers, json=body)
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == field


async def test_names_are_unique_per_practice_ignoring_case(client):
    t = await new_team(client)
    other = await new_team(client, "Other Practice")
    for name in ("Bug", "bug", "BUG", " bUg "):
        response = await client.post(tags_url(t), headers=t.owner.headers, json={"name": name})
        assert response.status_code == 409, name
        assert error(response)["code"] == "TAG_NAME_TAKEN"
        assert error(response)["details"][0]["field"] == "name"
    # Another practice may use any name.
    mine = await client.post(
        tags_url(other), headers=other.owner.headers, json={"name": "Brand new"}
    )
    assert mine.status_code == 201
    twin = await client.post(tags_url(t), headers=t.owner.headers, json={"name": "Brand new"})
    assert twin.status_code == 201


async def test_a_practice_holds_at_most_200_tags(client):
    t = await new_team(client)
    await sql(
        "INSERT INTO ticket_tags (id, organization_id, name, color) "
        "SELECT gen_random_uuid(), :o, 'Extra ' || n, 'grey' FROM generate_series(1, 193) AS n",
        o=t.id,
    )
    assert await sql(
        "SELECT count(*) AS n FROM ticket_tags WHERE organization_id = :o", o=t.id
    ) == [{"n": 200}]
    response = await client.post(
        tags_url(t), headers=t.owner.headers, json={"name": "One too many"}
    )
    assert response.status_code == 409 and error(response)["code"] == "TAG_LIMIT_REACHED"
    assert len(ok_data(await client.get(tags_url(t), headers=t.owner.headers))) == 200


async def test_simultaneous_creates_of_one_name_make_one_tag(client):
    t = await new_team(client)
    responses = await asyncio.gather(
        *[
            client.post(tags_url(t), headers=t.owner.headers, json={"name": "Race"})
            for _ in range(8)
        ]
    )
    assert sorted(r.status_code for r in responses) == [201] + [409] * 7
    assert await sql("SELECT count(*) AS n FROM ticket_tags WHERE name = 'Race'") == [{"n": 1}]


# --- Update ---------------------------------------------------------------------------------


async def test_rename_and_recolour_show_on_the_tickets(client):
    t = await new_team(client)
    bug = await tag_id(t.org, "Bug")
    ticket = await make_ticket(client, t.owner, t.org, tag_ids=[bug])
    response = await client.patch(
        tags_url(t, bug), headers=t.owner.headers, json={"name": "Defect", "color": "violet"}
    )
    data = ok_data(response)
    assert (data["name"], data["color"], data["ticket_count"]) == ("Defect", "violet", 1)
    assert response.json()["message"] == "Tag updated"
    shown = ok_data(await client.get(ticket_url(t.org, ticket["id"]), headers=t.owner.headers))
    assert shown["tags"] == [{"id": bug, "name": "Defect", "color": "violet"}]
    [entry] = await audit_entries(t.id, "ticket_tag.updated")
    assert entry["metadata"] == {
        "name": "Defect",
        "changes": {"name": ["Bug", "Defect"], "color": ["red", "violet"]},
    }


async def test_a_tag_may_change_the_case_of_its_own_name(client):
    t = await new_team(client)
    bug = await tag_id(t.org, "Bug")
    data = ok_data(
        await client.patch(tags_url(t, bug), headers=t.owner.headers, json={"name": "BUG"})
    )
    assert data["name"] == "BUG"


async def test_renaming_onto_another_tag_is_refused(client):
    t = await new_team(client)
    bug = await tag_id(t.org, "Bug")
    response = await client.patch(tags_url(t, bug), headers=t.owner.headers, json={"name": "ui"})
    assert response.status_code == 409 and error(response)["code"] == "TAG_NAME_TAKEN"
    assert await audit_entries(t.id, "ticket_tag.updated") == []


async def test_an_update_that_changes_nothing_writes_no_audit_row(client):
    t = await new_team(client)
    bug = await tag_id(t.org, "Bug")
    for body in ({}, {"name": "Bug"}, {"color": "red"}, {"name": "Bug", "color": "red"}):
        assert (
            await client.patch(tags_url(t, bug), headers=t.owner.headers, json=body)
        ).status_code == 200
    assert await audit_entries(t.id, "ticket_tag.updated") == []


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"name": None}, "name"),
        ({"color": None}, "color"),
        ({"name": ""}, "name"),
        ({"name": "x" * 41}, "name"),
        ({"color": "pink"}, "color"),
        ({"id": str(uuid.uuid4())}, "id"),
    ],
)
async def test_update_validation(client, body, field):
    t = await new_team(client)
    bug = await tag_id(t.org, "Bug")
    response = await client.patch(tags_url(t, bug), headers=t.owner.headers, json=body)
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == field


# --- Delete ---------------------------------------------------------------------------------


async def test_an_unused_tag_is_deleted(client):
    t = await new_team(client)
    validation = await tag_id(t.org, "Validation")
    response = await client.delete(tags_url(t, validation), headers=t.owner.headers)
    assert ok_data(response) == {"id": validation, "tickets_untagged": 0}
    assert response.json()["message"] == "Tag deleted"
    assert len(ok_data(await client.get(tags_url(t), headers=t.owner.headers))) == 6
    [entry] = await audit_entries(t.id, "ticket_tag.deleted")
    assert entry["target_id"] == validation
    assert entry["metadata"] == {"name": "Validation", "color": "red", "tickets_untagged": 0}
    assert (
        await client.delete(tags_url(t, validation), headers=t.owner.headers)
    ).status_code == 404


async def test_a_tag_in_use_needs_force(client):
    t = await new_team(client)
    bug, ui = await tag_id(t.org, "Bug"), await tag_id(t.org, "UI")
    a = await make_ticket(client, t.owner, t.org, "A", tag_ids=[bug, ui])
    b = await make_ticket(client, t.owner, t.org, "B", tag_ids=[bug])
    refused = await client.delete(tags_url(t, bug), headers=t.owner.headers)
    assert refused.status_code == 409
    assert error(refused)["code"] == "TAG_IN_USE"
    assert error(refused)["details"] == {"ticket_count": 2}
    assert "2 tickets" in error(refused)["message"]
    assert await audit_entries(t.id, "ticket_tag.deleted") == []

    forced = await client.delete(
        tags_url(t, bug), headers=t.owner.headers, params={"force": "true"}
    )
    assert ok_data(forced) == {"id": bug, "tickets_untagged": 2}
    for ticket, expected in ((a, ["UI"]), (b, [])):
        shown = ok_data(await client.get(ticket_url(t.org, ticket["id"]), headers=t.owner.headers))
        assert [tag["name"] for tag in shown["tags"]] == expected
    assert len(await sql("SELECT 1 FROM tickets")) == 2  # the tickets stay
    [entry] = await audit_entries(t.id, "ticket_tag.deleted")
    assert entry["metadata"]["tickets_untagged"] == 2


async def test_a_tag_on_one_ticket_is_in_use_in_the_singular(client):
    t = await new_team(client)
    bug = await tag_id(t.org, "Bug")
    await make_ticket(client, t.owner, t.org, tag_ids=[bug])
    refused = await client.delete(tags_url(t, bug), headers=t.owner.headers)
    assert "1 ticket." in error(refused)["message"]
    bad = await client.delete(
        tags_url(t, bug), headers=t.owner.headers, params={"force": "perhaps"}
    )
    assert bad.status_code == 422


# --- Permissions and tenancy ----------------------------------------------------------------

# role -> (list, create, update, delete)
EXPECTED_BY_ROLE = {
    "owner": (200, 201, 200, 200),
    "admin": (200, 201, 200, 200),
    "partner": (200, 201, 200, 200),
    "manager": (200, 201, 200, 200),
    "staff": (200, 201, 200, 403),
    "billing": (200, 201, 200, 403),
    "viewer": (200, 403, 403, 403),
}


@pytest.mark.parametrize("role", list(EXPECTED_BY_ROLE))
async def test_tag_permissions_per_system_role(client, role):
    t = await new_team(client)
    actor = await t.member(role)
    bug = await tag_id(t.org, "Bug")
    got = (
        (await client.get(tags_url(t), headers=actor.headers)).status_code,
        (
            await client.post(tags_url(t), headers=actor.headers, json={"name": f"By {role}"})
        ).status_code,
        (
            await client.patch(tags_url(t, bug), headers=actor.headers, json={"color": "green"})
        ).status_code,
        (await client.delete(tags_url(t, bug), headers=actor.headers)).status_code,
    )
    assert got == EXPECTED_BY_ROLE[role]


async def test_another_practices_tag_ids_are_not_found(client):
    mine = await new_team(client)
    theirs = await new_team(client, "Their Practice")
    foreign = await tag_id(theirs.org, "Bug")
    on_mine = tags_url(mine, foreign)
    assert (
        await client.patch(on_mine, headers=mine.owner.headers, json={"color": "green"})
    ).status_code == 404
    assert (await client.delete(on_mine, headers=mine.owner.headers)).status_code == 404
    on_theirs = tags_url(theirs, foreign)
    assert (
        await client.patch(on_theirs, headers=mine.owner.headers, json={"color": "green"})
    ).status_code == 404
    assert (await client.delete(on_theirs, headers=mine.owner.headers)).status_code == 404
    assert (
        await client.delete(tags_url(mine, uuid.uuid4()), headers=mine.owner.headers)
    ).status_code == 404
    still = [
        tag
        for tag in ok_data(await client.get(tags_url(theirs), headers=theirs.owner.headers))
        if tag["id"] == foreign
    ]
    assert still[0]["color"] == "red"

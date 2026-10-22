"""Ticket comments: writing, listing, editing (author only) and deleting (author or moderator)."""

import uuid

import pytest

from tests.clients_helpers import sql
from tests.factories import audit_entries, error
from tests.ticket_helpers import make_ticket, new_team, stored_files, ticket_url, upload
from tests.time_helpers import ok_data

COMMENT_KEYS = {
    "id",
    "ticket_id",
    "author",
    "body",
    "body_html",
    "edited_at",
    "created_at",
    "attachments",
}


def comments_url(t, ticket_id, comment_id=None) -> str:
    return ticket_url(t.org, ticket_id, "/comments" + (f"/{comment_id}" if comment_id else ""))


async def comment(client, t, actor, ticket_id, body="A comment", **extra) -> dict:
    response = await client.post(
        comments_url(t, ticket_id), headers=actor.headers, json={"body": body, **extra}
    )
    return ok_data(response, 201)


async def ticket_of(client, t):
    return await make_ticket(client, t.owner, t.org)


# --- Create ---------------------------------------------------------------------------------


async def test_add_a_comment(client):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await ticket_of(client, t)
    response = await client.post(
        comments_url(t, ticket["id"]),
        headers=staff.headers,
        json={"body": "  Reproduced on the staging site.\r\nSecond line  "},
    )
    data = ok_data(response, 201)
    assert set(data) == COMMENT_KEYS
    assert data["ticket_id"] == ticket["id"]
    assert data["author"] == {"id": str(staff.id), "full_name": staff.full_name}
    assert data["body"] == "Reproduced on the staging site.\nSecond line"
    assert data["edited_at"] is None and data["attachments"] == []
    assert response.json()["message"] == "Comment added"

    [entry] = await audit_entries(t.id, "ticket_comment.created")
    assert entry["target_type"] == "ticket_comment" and entry["target_id"] == data["id"]
    assert entry["actor_user_id"] == str(staff.id)
    assert entry["metadata"] == {
        "ticket_id": ticket["id"],
        "code": "APT1",
        "attachments": 0,
        "excerpt": "Reproduced on the staging site.\nSecond line",
    }

    shown = ok_data(await client.get(ticket_url(t.org, ticket["id"]), headers=t.owner.headers))
    assert shown["comment_count"] == 1


async def test_a_comment_may_be_5000_characters(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    assert (await comment(client, t, t.owner, ticket["id"], "x" * 5000))["body"] == "x" * 5000


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "body"),
        ({"body": ""}, "body"),
        ({"body": "   \n  "}, "body"),
        ({"body": "x" * 5001}, "body"),
        ({"body": "nul\u0000"}, "body"),
        ({"body": None}, "body"),
        ({"body": "ok", "extra": 1}, "extra"),
        ({"body": "ok", "attachment_ids": ["nope"]}, "attachment_ids.0"),
        ({"body": "ok", "attachment_ids": [str(uuid.uuid4())] * 26}, "attachment_ids"),
        ({"body": "ok", "attachment_ids": [str(uuid.uuid4())]}, "attachment_ids"),
    ],
)
async def test_create_validation(client, body, field):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    response = await client.post(comments_url(t, ticket["id"]), headers=t.owner.headers, json=body)
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == field
    assert await sql("SELECT 1 FROM ticket_comments") == []


async def test_a_comment_can_carry_files_uploaded_first(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    first = ok_data(await upload(client, t.owner, t.org, ticket["id"], "one.png"), 201)
    second = ok_data(await upload(client, t.owner, t.org, ticket["id"], "two.png"), 201)
    assert first["comment_id"] is None
    data = await comment(
        client,
        t,
        t.owner,
        ticket["id"],
        "See the screenshots",
        attachment_ids=[first["id"], second["id"], first["id"]],
    )
    assert [a["file_name"] for a in data["attachments"]] == ["one.png", "two.png"]
    assert {a["comment_id"] for a in data["attachments"]} == {data["id"]}
    shown = ok_data(await client.get(ticket_url(t.org, ticket["id"]), headers=t.owner.headers))
    assert {a["comment_id"] for a in shown["attachments"]} == {data["id"]}
    assert shown["attachment_count"] == 2
    [entry] = await audit_entries(t.id, "ticket_comment.created")
    assert entry["metadata"]["attachments"] == 2


async def test_a_comment_without_text_needs_files(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    pasted = ok_data(await upload(client, t.owner, t.org, ticket["id"], "pasted.png"), 201)
    response = await client.post(
        comments_url(t, ticket["id"]),
        headers=t.owner.headers,
        json={"body": "", "attachment_ids": [pasted["id"]]},
    )
    data = ok_data(response, 201)
    assert data["body"] == "" and len(data["attachments"]) == 1
    only_files = await client.post(
        comments_url(t, ticket["id"]),
        headers=t.owner.headers,
        json={"attachment_ids": [pasted["id"]]},
    )
    assert only_files.status_code == 422  # it is on the first comment now


async def test_only_loose_files_of_the_same_ticket_can_be_attached(client):
    t = await new_team(client)
    ticket, other = await ticket_of(client, t), await ticket_of(client, t)
    elsewhere = ok_data(await upload(client, t.owner, t.org, other["id"]), 201)
    earlier = await comment(client, t, t.owner, ticket["id"], "Earlier")
    on_comment = ok_data(
        await upload(client, t.owner, t.org, ticket["id"], comment_id=earlier["id"]), 201
    )
    for bad in (elsewhere["id"], on_comment["id"], str(uuid.uuid4())):
        response = await client.post(
            comments_url(t, ticket["id"]),
            headers=t.owner.headers,
            json={"body": "x", "attachment_ids": [bad]},
        )
        assert response.status_code == 422, bad
        assert error(response)["details"][0]["field"] == "attachment_ids"
    # Not even a partial attach: the loose file next to a bad id stays loose.
    loose = ok_data(await upload(client, t.owner, t.org, ticket["id"], "loose.png"), 201)
    refused = await client.post(
        comments_url(t, ticket["id"]),
        headers=t.owner.headers,
        json={"body": "x", "attachment_ids": [loose["id"], elsewhere["id"]]},
    )
    assert refused.status_code == 422
    rows = await sql("SELECT comment_id FROM ticket_attachments WHERE id = :a", a=loose["id"])
    assert rows == [{"comment_id": None}]


# --- List -----------------------------------------------------------------------------------


async def test_comments_are_listed_oldest_first_with_authors_and_files(client):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await ticket_of(client, t)
    first = await comment(client, t, t.owner, ticket["id"], "First")
    second = await comment(client, t, staff, ticket["id"], "Second")
    assert (
        await upload(
            client,
            staff,
            t.org,
            ticket["id"],
            "log.csv",
            b"a,b\n1,2\n",
            "text/csv",
            comment_id=second["id"],
        )
    ).status_code == 201
    response = await client.get(comments_url(t, ticket["id"]), headers=t.owner.headers)
    data = ok_data(response)
    assert [c["id"] for c in data] == [first["id"], second["id"]]
    assert all(set(c) == COMMENT_KEYS for c in data)
    assert data[0]["author"]["full_name"] == t.owner.full_name
    assert data[1]["author"]["id"] == str(staff.id)
    assert data[0]["attachments"] == []
    [file] = data[1]["attachments"]
    assert (file["file_name"], file["comment_id"], file["uploaded_by"]) == (
        "log.csv",
        second["id"],
        str(staff.id),
    )
    assert response.json()["meta"] == {"next_cursor": None, "has_more": False}


async def test_comments_are_paginated(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    ids = [
        (await comment(client, t, t.owner, ticket["id"], f"Comment {i}"))["id"] for i in range(5)
    ]
    seen, cursor, calls = [], None, 0
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = (
            await client.get(comments_url(t, ticket["id"]), headers=t.owner.headers, params=params)
        ).json()
        seen += [c["id"] for c in body["data"]]
        calls += 1
        cursor = body["meta"]["next_cursor"]
        if not cursor:
            break
    assert seen == ids and calls == 3
    bad = await client.get(
        comments_url(t, ticket["id"]), headers=t.owner.headers, params={"cursor": "garbage"}
    )
    assert bad.status_code == 422


async def test_comments_of_an_unknown_or_foreign_ticket_are_not_found(client):
    mine = await new_team(client)
    theirs = await new_team(client, "Their Practice")
    foreign = await make_ticket(client, theirs.owner, theirs.org)
    await comment(client, theirs, theirs.owner, foreign["id"], "Secret")
    for ticket_id in (uuid.uuid4(), foreign["id"]):
        listed = await client.get(comments_url(mine, ticket_id), headers=mine.owner.headers)
        assert listed.status_code == 404
        written = await client.post(
            comments_url(mine, ticket_id), headers=mine.owner.headers, json={"body": "x"}
        )
        assert written.status_code == 404
    on_theirs = await client.get(comments_url(theirs, foreign["id"]), headers=mine.owner.headers)
    assert on_theirs.status_code == 404
    assert await sql("SELECT count(*) AS n FROM ticket_comments") == [{"n": 1}]


# --- Edit -----------------------------------------------------------------------------------


async def test_the_author_edits_a_comment(client):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await ticket_of(client, t)
    written = await comment(client, t, staff, ticket["id"], "Typo here")
    url = comments_url(t, ticket["id"], written["id"])
    response = await client.patch(url, headers=staff.headers, json={"body": " Fixed text "})
    data = ok_data(response)
    assert set(data) == COMMENT_KEYS
    assert data["body"] == "Fixed text" and data["edited_at"] is not None
    assert data["created_at"] == written["created_at"]
    assert response.json()["message"] == "Comment updated"
    [entry] = await audit_entries(t.id, "ticket_comment.updated")
    assert entry["target_id"] == written["id"] and entry["actor_user_id"] == str(staff.id)
    assert entry["metadata"] == {
        "ticket_id": ticket["id"],
        "code": "APT1",
        "changes": {"body": ["Typo here", "Fixed text"]},
    }


async def test_an_edit_that_changes_nothing_is_not_an_edit(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    written = await comment(client, t, t.owner, ticket["id"], "Same")
    data = ok_data(
        await client.patch(
            comments_url(t, ticket["id"], written["id"]),
            headers=t.owner.headers,
            json={"body": "Same"},
        )
    )
    assert data["edited_at"] is None
    assert await audit_entries(t.id, "ticket_comment.updated") == []


async def test_only_the_author_may_edit_even_a_moderator(client):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await ticket_of(client, t)
    written = await comment(client, t, staff, ticket["id"], "Mine")
    url = comments_url(t, ticket["id"], written["id"])
    for role in ("owner", "admin", "manager", "billing", "viewer"):
        actor = await t.member(role)
        response = await client.patch(url, headers=actor.headers, json={"body": "Rewritten"})
        assert response.status_code == 403, role
    rows = await sql("SELECT body, edited_at FROM ticket_comments WHERE id = :c", c=written["id"])
    assert rows == [{"body": "Mine", "edited_at": None}]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "body"),
        ({"body": None}, "body"),
        ({"body": "x" * 5001}, "body"),
        ({"body": "nul\u0000"}, "body"),
        ({"body": "ok", "attachment_ids": []}, "attachment_ids"),
    ],
)
async def test_edit_validation(client, body, field):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    written = await comment(client, t, t.owner, ticket["id"], "Original")
    response = await client.patch(
        comments_url(t, ticket["id"], written["id"]), headers=t.owner.headers, json=body
    )
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == field


async def test_the_text_may_be_emptied_only_while_the_comment_has_files(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    bare = await comment(client, t, t.owner, ticket["id"], "Words only")
    refused = await client.patch(
        comments_url(t, ticket["id"], bare["id"]), headers=t.owner.headers, json={"body": ""}
    )
    assert refused.status_code == 422 and error(refused)["details"][0]["field"] == "body"
    assert (
        await upload(client, t.owner, t.org, ticket["id"], comment_id=bare["id"])
    ).status_code == 201
    emptied = await client.patch(
        comments_url(t, ticket["id"], bare["id"]), headers=t.owner.headers, json={"body": ""}
    )
    assert ok_data(emptied)["body"] == ""


async def test_a_comment_is_found_only_under_its_own_ticket(client):
    t = await new_team(client)
    ticket, other = await ticket_of(client, t), await ticket_of(client, t)
    written = await comment(client, t, t.owner, ticket["id"])
    for call in (
        client.patch(
            comments_url(t, other["id"], written["id"]), headers=t.owner.headers, json={"body": "x"}
        ),
        client.delete(comments_url(t, other["id"], written["id"]), headers=t.owner.headers),
        client.patch(
            comments_url(t, ticket["id"], uuid.uuid4()), headers=t.owner.headers, json={"body": "x"}
        ),
        client.delete(comments_url(t, ticket["id"], uuid.uuid4()), headers=t.owner.headers),
    ):
        assert (await call).status_code == 404


# --- Delete ---------------------------------------------------------------------------------


async def test_the_author_deletes_a_comment_and_its_files(client):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await ticket_of(client, t)
    written = await comment(client, t, staff, ticket["id"], "Mine")
    kept = ok_data(await upload(client, staff, t.org, ticket["id"], "kept.png"), 201)
    assert (
        await upload(client, staff, t.org, ticket["id"], "gone.png", comment_id=written["id"])
    ).status_code == 201
    assert len(stored_files()) == 2

    response = await client.delete(
        comments_url(t, ticket["id"], written["id"]), headers=staff.headers
    )
    assert response.status_code == 200 and response.json()["message"] == "Comment deleted"
    assert len(stored_files()) == 1
    rows = await sql("SELECT id FROM ticket_attachments")
    assert [str(r["id"]) for r in rows] == [kept["id"]]
    assert await sql("SELECT 1 FROM ticket_comments") == []
    shown = ok_data(await client.get(ticket_url(t.org, ticket["id"]), headers=t.owner.headers))
    assert shown["comment_count"] == 0 and shown["attachment_count"] == 1
    [entry] = await audit_entries(t.id, "ticket_comment.deleted")
    assert entry["metadata"] == {"ticket_id": ticket["id"], "code": "APT1", "attachments": 1}
    assert (
        await client.delete(comments_url(t, ticket["id"], written["id"]), headers=staff.headers)
    ).status_code == 404


async def test_a_moderator_deletes_anyones_comment_but_staff_only_their_own(client):
    t = await new_team(client)
    alice = await t.member("staff")
    ticket = await ticket_of(client, t)
    theirs = await comment(client, t, t.owner, ticket["id"], "By the owner")
    mine = await comment(client, t, alice, ticket["id"], "By alice")
    assert (
        await client.delete(comments_url(t, ticket["id"], theirs["id"]), headers=alice.headers)
    ).status_code == 403
    manager = await t.member("manager")
    assert (
        await client.delete(comments_url(t, ticket["id"], mine["id"]), headers=manager.headers)
    ).status_code == 200
    assert (
        await client.delete(comments_url(t, ticket["id"], theirs["id"]), headers=alice.headers)
    ).status_code == 403
    remaining = await sql("SELECT id FROM ticket_comments")
    assert [str(r["id"]) for r in remaining] == [theirs["id"]]
    [entry] = await audit_entries(t.id, "ticket_comment.deleted")
    assert entry["actor_user_id"] == str(manager.id) and entry["target_id"] == mine["id"]


# --- Permissions ----------------------------------------------------------------------------

# role -> (list, create, edit own, delete own, delete the owner's)
EXPECTED_BY_ROLE = {
    "owner": (200, 201, 200, 200, 200),
    "admin": (200, 201, 200, 200, 200),
    "partner": (200, 201, 200, 200, 200),
    "manager": (200, 201, 200, 200, 200),
    "staff": (200, 201, 200, 200, 403),
    "billing": (200, 201, 200, 200, 403),
    "viewer": (200, 403, None, None, 403),
}


@pytest.mark.parametrize("role", list(EXPECTED_BY_ROLE))
async def test_comment_permissions_per_system_role(client, role):
    t = await new_team(client)
    actor = await t.member(role)
    ticket = await ticket_of(client, t)
    by_owner = await comment(client, t, t.owner, ticket["id"], "Owner's")
    listed = (await client.get(comments_url(t, ticket["id"]), headers=actor.headers)).status_code
    created = await client.post(
        comments_url(t, ticket["id"]), headers=actor.headers, json={"body": "Mine"}
    )
    edited = deleted = None
    if created.status_code == 201:
        own = created.json()["data"]["id"]
        edited = (
            await client.patch(
                comments_url(t, ticket["id"], own), headers=actor.headers, json={"body": "Edited"}
            )
        ).status_code
        deleted = (
            await client.delete(comments_url(t, ticket["id"], own), headers=actor.headers)
        ).status_code
    others = (
        await client.delete(comments_url(t, ticket["id"], by_owner["id"]), headers=actor.headers)
    ).status_code
    expected = EXPECTED_BY_ROLE[role]
    assert (listed, created.status_code, edited, deleted, others) == expected

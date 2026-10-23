"""A ticket's timeline: what happened to it, read from the audit log."""

from tests.ticket_helpers import (
    add_tag,
    make_ticket,
    new_team,
    tag_id,
    ticket_url,
    upload,
)
from tests.time_helpers import ok_data


async def timeline(client, t, ticket_id, actor=None, **params):
    response = await client.get(
        ticket_url(t.org, ticket_id, "/timeline"), headers=(actor or t.owner).headers, params=params
    )
    return response


async def events(client, t, ticket_id, actor=None, **params):
    return ok_data(await timeline(client, t, ticket_id, actor, **params))


def kinds(found):
    """Oldest first, as the story reads."""
    return [e["kind"] for e in reversed(found)]


async def test_a_new_ticket_starts_its_timeline(client):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org, "Crash", status="blocked")
    found = await events(client, t, ticket["id"])
    assert [(e["kind"], e["after"], e["before"]) for e in found] == [("created", "blocked", None)]
    assert found[0]["actor"]["id"] == str(t.owner.id)
    assert found[0]["bulk"] is False and found[0]["added"] == [] and found[0]["removed"] == []


async def test_every_kind_of_change_is_an_event(client):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await make_ticket(client, t.owner, t.org, "Crash")
    url = ticket_url(t.org, ticket["id"])
    await add_tag(t.org, "Docs")

    async def patch(**body):
        assert (await client.patch(url, headers=t.owner.headers, json=body)).status_code == 200

    await patch(status="requirement_pending")
    await patch(priority="high")
    await patch(assignee_id=str(staff.id))
    await patch(assignee_id=None)
    await patch(tag_ids=[await tag_id(t.org, "Bug")])
    await patch(tag_ids=[await tag_id(t.org, "Docs")])
    await patch(status="ready_for_ba_review")

    found = await events(client, t, ticket["id"])
    assert kinds(found) == [
        "created",
        "status_changed",
        "priority_changed",
        "assignee_changed",
        "assignee_changed",
        "tags_changed",
        "tags_changed",
        "status_changed",
    ]
    story = list(reversed(found))
    assert (story[1]["before"], story[1]["after"]) == ("open", "requirement_pending")
    assert (story[2]["before"], story[2]["after"]) == ("not_set", "high")
    assert (story[3]["before"], story[3]["after"]) == ("Unassigned", staff.full_name)
    assert (story[4]["before"], story[4]["after"]) == (staff.full_name, "Unassigned")
    assert (story[5]["added"], story[5]["removed"]) == (["Bug"], [])
    assert (story[6]["added"], story[6]["removed"]) == (["Docs"], ["Bug"])
    assert (story[7]["before"], story[7]["after"]) == ("requirement_pending", "ready_for_ba_review")
    assert len({e["id"] for e in found}) == len(found)
    # Newest first, each with the person who did it.
    assert found[0]["kind"] == "status_changed"
    assert all(e["actor"]["id"] == str(t.owner.id) for e in found)


async def test_one_edit_of_several_fields_is_several_events(client):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await make_ticket(client, t.owner, t.org, "Crash")
    patched = await client.patch(
        ticket_url(t.org, ticket["id"]),
        headers=t.owner.headers,
        json={"status": "invalid", "priority": "low", "assignee_id": str(staff.id)},
    )
    assert patched.status_code == 200
    story = list(reversed(await events(client, t, ticket["id"])))
    assert sorted(kinds(story[::-1])[1:]) == [
        "assignee_changed",
        "priority_changed",
        "status_changed",
    ]


async def test_comments_and_files_are_events(client):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await make_ticket(client, t.owner, t.org, "Crash")
    comments = ticket_url(t.org, ticket["id"], "/comments")
    made = ok_data(
        await client.post(comments, headers=staff.headers, json={"body": "Seen on Safari too"}), 201
    )
    uploaded = await upload(client, staff, t.org, ticket["id"], "shot.png", comment_id=made["id"])
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["data"]["id"]
    removed = await client.delete(
        ticket_url(t.org, ticket["id"], f"/attachments/{file_id}"), headers=staff.headers
    )
    assert removed.status_code in (200, 204)

    found = await events(client, t, ticket["id"])
    assert kinds(found) == [
        "created",
        "comment_added",
        "file_added",
        "file_removed",
    ]
    story = list(reversed(found))
    assert story[1]["detail"] == "Seen on Safari too" and story[1]["comment_id"] == made["id"]
    assert story[2]["detail"] == "shot.png" and story[2]["comment_id"] == made["id"]
    assert story[3]["detail"] == "shot.png"
    assert all(e["actor"]["id"] == str(staff.id) for e in story[1:])


async def test_a_bulk_change_is_marked(client):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org, "Crash")
    done = await client.post(
        ticket_url(t.org, tail="/bulk"),
        headers=t.owner.headers,
        json={"action": "set_status", "ticket_ids": [ticket["id"]], "value": "on_hold"},
    )
    assert done.status_code == 200, done.text
    found = await events(client, t, ticket["id"])
    assert (found[0]["kind"], found[0]["after"], found[0]["bulk"]) == (
        "status_changed",
        "on_hold",
        True,
    )


async def test_the_timeline_belongs_to_one_ticket(client):
    t = await new_team(client)
    first = await make_ticket(client, t.owner, t.org, "First")
    second = await make_ticket(client, t.owner, t.org, "Second")
    await client.patch(
        ticket_url(t.org, second["id"]), headers=t.owner.headers, json={"priority": "high"}
    )
    await client.post(
        ticket_url(t.org, second["id"], "/comments"), headers=t.owner.headers, json={"body": "hi"}
    )
    assert kinds(await events(client, t, first["id"])) == ["created"]
    assert len(await events(client, t, second["id"])) == 3


async def test_it_pages_newest_first(client):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org, "Crash")
    for status in ("in_progress", "blocked", "on_hold"):
        await client.patch(
            ticket_url(t.org, ticket["id"]), headers=t.owner.headers, json={"status": status}
        )
    first = (await timeline(client, t, ticket["id"], limit=2)).json()
    assert [e["after"] for e in first["data"]] == ["on_hold", "blocked"]
    assert first["meta"]["has_more"] is True
    rest = (
        await timeline(client, t, ticket["id"], limit=2, cursor=first["meta"]["next_cursor"])
    ).json()
    assert [e["kind"] for e in rest["data"]] == ["status_changed", "created"]
    assert rest["meta"]["has_more"] is False


async def test_reading_needs_ticket_read_and_the_ticket_must_exist(client):
    t = await new_team(client)
    viewer = await t.member("viewer")
    ticket = await make_ticket(client, t.owner, t.org, "Crash")
    # A viewer reads tickets, so reads the timeline, without audit_log.read.
    assert (await timeline(client, t, ticket["id"], viewer)).status_code == 200
    missing = "00000000-0000-4000-8000-000000000000"
    assert (await timeline(client, t, missing)).status_code == 404
    other = await new_team(client, "Other Practice")
    foreign = await make_ticket(client, other.owner, other.org, "Theirs")
    assert (await timeline(client, t, foreign["id"])).status_code == 404

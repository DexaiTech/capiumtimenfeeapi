"""Ticket attachments: uploading, listing, downloading and deleting files; limits, permissions,
tenancy, audit rows and what happens to the files in the storage."""

import asyncio
import hashlib
import uuid
from urllib.parse import quote

import pytest

from app.repositories.ticket_files import TicketFileRepository
from app.services.storage import LocalFileStorage, StorageError
from tests.conftest import STORAGE_DIR
from tests.factories import audit_entries, error
from tests.ticket_helpers import (
    DOCX_TYPE,
    SAMPLES,
    XLSX_TYPE,
    attachment_rows,
    make_ticket,
    new_team,
    stored_files,
    ticket_url,
    upload,
)
from tests.time_helpers import ok_data

ATTACHMENT_KEYS = {
    "id",
    "ticket_id",
    "comment_id",
    "file_name",
    "content_type",
    "size_bytes",
    "is_image",
    "uploaded_by",
    "uploaded_by_name",
    "created_at",
}
DETECTED = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "docx": DOCX_TYPE,
    "xlsx": XLSX_TYPE,
    "doc": "application/msword",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
}
IMAGES = {"png", "jpg", "jpeg", "gif", "webp"}


def files_url(t, ticket_id, attachment_id=None) -> str:
    return ticket_url(
        t.org, ticket_id, "/attachments" + (f"/{attachment_id}" if attachment_id else "")
    )


async def up(client, t, actor, ticket_id, kind="png", **kwargs) -> dict:
    sample = SAMPLES[kind]
    response = await upload(
        client, actor, t.org, ticket_id, sample.name, sample.data, sample.content_type, **kwargs
    )
    return ok_data(response, 201)


async def ticket_of(client, t):
    return await make_ticket(client, t.owner, t.org)


# --- Upload ---------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(DETECTED))
async def test_every_allowed_type_can_be_uploaded_and_downloaded(client, kind):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await ticket_of(client, t)
    sample = SAMPLES[kind]
    response = await upload(
        client, staff, t.org, ticket["id"], sample.name, sample.data, sample.content_type
    )
    data = ok_data(response, 201)
    assert set(data) == ATTACHMENT_KEYS
    assert response.json()["message"] == "File uploaded"
    assert data["ticket_id"] == ticket["id"] and data["comment_id"] is None
    assert data["file_name"] == sample.name
    assert data["content_type"] == DETECTED[kind]
    assert data["size_bytes"] == len(sample.data)
    assert data["is_image"] is (kind in IMAGES)
    assert data["uploaded_by"] == str(staff.id) and data["uploaded_by_name"] == staff.full_name

    [row] = await attachment_rows(ticket["id"])
    assert row["sha256"] == hashlib.sha256(sample.data).hexdigest()
    assert row["content_type"] == DETECTED[kind] and row["size_bytes"] == len(sample.data)

    [stored] = stored_files()
    assert stored.read_bytes() == sample.data
    # Where it lives: the practice's directory, a two-character folder, the random key.
    assert stored.relative_to(STORAGE_DIR).parts == (
        t.id,
        row["storage_key"][:2],
        row["storage_key"],
    )

    got = await client.get(files_url(t, ticket["id"], data["id"]), headers=t.owner.headers)
    assert got.status_code == 200
    assert got.content == sample.data
    assert got.headers["content-type"].split(";")[0] == DETECTED[kind]
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-length"] == str(len(sample.data))
    assert got.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in got.headers["content-security-policy"]
    disposition = got.headers["content-disposition"]
    assert disposition == (
        f"{'inline' if kind in IMAGES else 'attachment'}; "
        f"filename=\"{sample.name}\"; filename*=UTF-8''{sample.name}"
    )


async def test_storage_keys_are_random_and_not_derived_from_the_name(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    for _ in range(3):
        await up(client, t, t.owner, ticket["id"])
    keys = [row["storage_key"] for row in await attachment_rows()]
    assert len(set(keys)) == 3
    assert all(len(k) == 43 and "shot" not in k and "." not in k for k in keys)
    assert {p.name for p in stored_files()} == set(keys)


async def test_a_non_ascii_name_is_sent_in_both_header_forms(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    sample = SAMPLES["pdf"]
    # e acute, en dash and curly quotes.
    name = "Caf\u00e9 report \u2013 \u201cQ3\u201d.pdf"
    data = ok_data(
        await upload(client, t.owner, t.org, ticket["id"], name, sample.data, "application/pdf"),
        201,
    )
    assert data["file_name"] == name
    got = await client.get(files_url(t, ticket["id"], data["id"]), headers=t.owner.headers)
    assert got.headers["content-disposition"] == (
        f"attachment; filename=\"Caf_ report _ _Q3_.pdf\"; filename*=UTF-8''{quote(name, safe='')}"
    )


async def test_an_upload_is_audited(client):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await ticket_of(client, t)
    data = await up(client, t, staff, ticket["id"], "pdf")
    [entry] = await audit_entries(t.id, "ticket_attachment.added")
    assert entry["target_type"] == "ticket_attachment" and entry["target_id"] == data["id"]
    assert entry["actor_user_id"] == str(staff.id)
    assert entry["metadata"] == {
        "ticket_id": ticket["id"],
        "code": "APT1",
        "file_name": "report.pdf",
        "content_type": "application/pdf",
        "size_bytes": len(SAMPLES["pdf"].data),
        "comment_id": None,
    }


# --- Putting a file on a comment ------------------------------------------------------------


async def test_a_file_goes_on_a_comment_by_form_field_or_query_parameter(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    created = await client.post(
        files_url(t, ticket["id"]).replace("/attachments", "/comments"),
        headers=t.owner.headers,
        json={"body": "Evidence"},
    )
    comment_id = created.json()["data"]["id"]
    by_form = await up(client, t, t.owner, ticket["id"], comment_id=comment_id)
    by_query = await up(
        client, t, t.owner, ticket["id"], comment_id=comment_id, comment_in_form=False
    )
    assert by_form["comment_id"] == by_query["comment_id"] == comment_id
    listed = ok_data(await client.get(files_url(t, ticket["id"]), headers=t.owner.headers))
    assert [a["comment_id"] for a in listed] == [comment_id, comment_id]
    [entry, _] = await audit_entries(t.id, "ticket_attachment.added")
    assert entry["metadata"]["comment_id"] == comment_id


async def test_the_comment_must_be_the_tickets_and_the_caller_must_own_it_or_moderate(client):
    t = await new_team(client)
    ticket, other = await ticket_of(client, t), await ticket_of(client, t)
    alice = await t.member("staff")
    manager = await t.member("manager")
    theirs = (
        await client.post(
            files_url(t, ticket["id"]).replace("/attachments", "/comments"),
            headers=t.owner.headers,
            json={"body": "Owner's"},
        )
    ).json()["data"]
    elsewhere = (
        await client.post(
            files_url(t, other["id"]).replace("/attachments", "/comments"),
            headers=t.owner.headers,
            json={"body": "On the other ticket"},
        )
    ).json()["data"]
    sample = SAMPLES["png"]

    async def post(actor, comment_id):
        return await upload(
            client,
            actor,
            t.org,
            ticket["id"],
            sample.name,
            sample.data,
            "image/png",
            comment_id=comment_id,
        )

    wrong_ticket = await post(t.owner, elsewhere["id"])
    assert wrong_ticket.status_code == 422
    assert error(wrong_ticket)["details"][0]["field"] == "comment_id"
    assert (await post(t.owner, uuid.uuid4())).status_code == 422
    assert (await post(alice, theirs["id"])).status_code == 403
    assert (await post(manager, theirs["id"])).status_code == 201  # ticket.delete: moderation
    assert (await post(t.owner, theirs["id"])).status_code == 201
    assert len(stored_files()) == 2


async def test_a_comment_id_that_is_not_an_id_or_is_given_twice_differently_is_refused(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    sample = SAMPLES["png"]
    bad = await client.post(
        files_url(t, ticket["id"]),
        headers=t.owner.headers,
        files={"file": (sample.name, sample.data, "image/png")},
        data={"comment_id": "nope"},
    )
    assert bad.status_code == 422 and error(bad)["details"][0]["field"] == "comment_id"
    twice = await client.post(
        files_url(t, ticket["id"]),
        headers=t.owner.headers,
        files={"file": (sample.name, sample.data, "image/png")},
        data={"comment_id": str(uuid.uuid4())},
        params={"comment_id": str(uuid.uuid4())},
    )
    assert twice.status_code == 422 and error(twice)["details"][0]["field"] == "comment_id"
    blank = await client.post(
        files_url(t, ticket["id"]),
        headers=t.owner.headers,
        files={"file": (sample.name, sample.data, "image/png")},
        data={"comment_id": ""},
    )
    assert blank.status_code == 201 and ok_data(blank, 201)["comment_id"] is None
    assert len(stored_files()) == 1


# --- Listing --------------------------------------------------------------------------------


async def test_the_files_of_a_ticket_are_listed_oldest_first(client):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await ticket_of(client, t)
    first = await up(client, t, t.owner, ticket["id"], "png")
    second = await up(client, t, staff, ticket["id"], "pdf")
    third = await up(client, t, t.owner, ticket["id"], "csv")
    data = ok_data(await client.get(files_url(t, ticket["id"]), headers=staff.headers))
    assert [a["id"] for a in data] == [first["id"], second["id"], third["id"]]
    assert all(set(a) == ATTACHMENT_KEYS for a in data)
    assert data[1]["uploaded_by_name"] == staff.full_name
    shown = ok_data(await client.get(ticket_url(t.org, ticket["id"]), headers=staff.headers))
    assert [a["id"] for a in shown["attachments"]] == [first["id"], second["id"], third["id"]]
    assert shown["attachment_count"] == 3


async def test_a_ticket_holds_at_most_25_files(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    for _ in range(25):
        await up(client, t, t.owner, ticket["id"])
    sample = SAMPLES["png"]
    response = await upload(
        client, t.owner, t.org, ticket["id"], sample.name, sample.data, "image/png"
    )
    assert response.status_code == 409
    assert error(response)["code"] == "ATTACHMENT_LIMIT_REACHED"
    assert error(response)["details"] == {"max_attachments": 25}
    assert len(stored_files()) == 25 and len(await attachment_rows()) == 25
    # Another ticket has its own 25.
    other = await ticket_of(client, t)
    assert (await upload(client, t.owner, t.org, other["id"])).status_code == 201


async def test_two_uploads_cannot_both_take_the_last_place(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    for _ in range(24):
        await up(client, t, t.owner, ticket["id"])
    responses = await asyncio.gather(
        *[upload(client, t.owner, t.org, ticket["id"]) for _ in range(4)]
    )
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]
    assert len(await attachment_rows()) == 25 and len(stored_files()) == 25


# --- Delete ---------------------------------------------------------------------------------


async def test_the_uploader_deletes_a_file_and_it_leaves_the_storage(client):
    t = await new_team(client)
    staff = await t.member("staff")
    ticket = await ticket_of(client, t)
    kept = await up(client, t, staff, ticket["id"], "pdf")
    gone = await up(client, t, staff, ticket["id"], "png")
    assert len(stored_files()) == 2
    response = await client.delete(files_url(t, ticket["id"], gone["id"]), headers=staff.headers)
    assert response.status_code == 200 and response.json()["message"] == "File deleted"
    remaining = ok_data(await client.get(files_url(t, ticket["id"]), headers=staff.headers))
    assert [a["id"] for a in remaining] == [kept["id"]]
    [file] = stored_files()
    assert file.read_bytes() == SAMPLES["pdf"].data
    [entry] = await audit_entries(t.id, "ticket_attachment.deleted")
    assert entry["target_id"] == gone["id"] and entry["actor_user_id"] == str(staff.id)
    assert entry["metadata"] == {
        "ticket_id": ticket["id"],
        "code": "APT1",
        "file_name": "shot.png",
        "size_bytes": len(SAMPLES["png"].data),
    }
    again = await client.delete(files_url(t, ticket["id"], gone["id"]), headers=staff.headers)
    assert again.status_code == 404


async def test_only_the_uploader_or_a_moderator_deletes_a_file(client):
    t = await new_team(client)
    alice, bob = await t.member("staff"), await t.member("billing")
    manager, viewer = await t.member("manager"), await t.member("viewer")
    ticket = await ticket_of(client, t)
    mine = await up(client, t, alice, ticket["id"])
    assert (
        await client.delete(files_url(t, ticket["id"], mine["id"]), headers=bob.headers)
    ).status_code == 403
    assert (
        await client.delete(files_url(t, ticket["id"], mine["id"]), headers=viewer.headers)
    ).status_code == 403
    assert len(stored_files()) == 1
    assert (
        await client.delete(files_url(t, ticket["id"], mine["id"]), headers=manager.headers)
    ).status_code == 200
    assert stored_files() == []
    theirs = await up(client, t, manager, ticket["id"])
    assert (
        await client.delete(files_url(t, ticket["id"], theirs["id"]), headers=alice.headers)
    ).status_code == 403


async def test_the_last_file_of_a_comment_without_text_cannot_be_deleted(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    loose = await up(client, t, t.owner, ticket["id"])
    comment = ok_data(
        await client.post(
            files_url(t, ticket["id"]).replace("/attachments", "/comments"),
            headers=t.owner.headers,
            json={"body": "", "attachment_ids": [loose["id"]]},
        ),
        201,
    )
    refused = await client.delete(files_url(t, ticket["id"], loose["id"]), headers=t.owner.headers)
    assert refused.status_code == 409 and error(refused)["code"] == "COMMENT_WOULD_BE_EMPTY"
    assert len(stored_files()) == 1
    second = await up(client, t, t.owner, ticket["id"], comment_id=comment["id"])
    assert (
        await client.delete(files_url(t, ticket["id"], loose["id"]), headers=t.owner.headers)
    ).status_code == 200
    still = await client.delete(files_url(t, ticket["id"], second["id"]), headers=t.owner.headers)
    assert still.status_code == 409
    # With text on the comment the file may go.
    edited = await client.patch(
        files_url(t, ticket["id"]).replace("/attachments", f"/comments/{comment['id']}"),
        headers=t.owner.headers,
        json={"body": "Now with words"},
    )
    assert edited.status_code == 200
    assert (
        await client.delete(files_url(t, ticket["id"], second["id"]), headers=t.owner.headers)
    ).status_code == 200
    assert stored_files() == []


async def test_a_file_that_cannot_be_removed_does_not_fail_the_delete(client, monkeypatch):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    data = await up(client, t, t.owner, ticket["id"])

    async def stuck(self, organization_id, key):
        raise StorageError("read-only")

    monkeypatch.setattr(LocalFileStorage, "delete", stuck)
    response = await client.delete(files_url(t, ticket["id"], data["id"]), headers=t.owner.headers)
    assert response.status_code == 200
    assert await attachment_rows() == []
    assert len(stored_files()) == 1  # an orphan: logged, nothing the caller can do


# --- Download -------------------------------------------------------------------------------


async def test_a_file_the_store_lost_answers_404_with_a_stable_code(client):
    t = await new_team(client)
    ticket = await ticket_of(client, t)
    data = await up(client, t, t.owner, ticket["id"])
    stored_files()[0].unlink()
    response = await client.get(files_url(t, ticket["id"], data["id"]), headers=t.owner.headers)
    assert response.status_code == 404
    assert error(response)["code"] == "ATTACHMENT_FILE_MISSING"


async def test_unknown_files_are_not_found_and_ids_must_be_ids(client):
    t = await new_team(client)
    ticket, other = await ticket_of(client, t), await ticket_of(client, t)
    data = await up(client, t, t.owner, ticket["id"])
    for call in (
        client.get(files_url(t, ticket["id"], uuid.uuid4()), headers=t.owner.headers),
        client.get(
            files_url(t, other["id"], data["id"]), headers=t.owner.headers
        ),  # not its ticket
        client.delete(files_url(t, other["id"], data["id"]), headers=t.owner.headers),
        client.get(files_url(t, uuid.uuid4(), data["id"]), headers=t.owner.headers),
        client.get(files_url(t, uuid.uuid4()), headers=t.owner.headers),
    ):
        assert (await call).status_code == 404
    assert (
        await client.get(files_url(t, ticket["id"], "nope"), headers=t.owner.headers)
    ).status_code == 422
    assert len(stored_files()) == 1


# --- Failures leave nothing behind ----------------------------------------------------------


async def test_a_failure_after_the_file_was_stored_removes_it_again(client, monkeypatch):
    t = await new_team(client)
    ticket = await ticket_of(client, t)

    async def boom(self, attachment):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(TicketFileRepository, "add_attachment", boom)
    response = await upload(client, t.owner, t.org, ticket["id"])
    assert response.status_code == 500
    monkeypatch.undo()
    assert stored_files() == [] and await attachment_rows() == []
    assert (await upload(client, t.owner, t.org, ticket["id"])).status_code == 201


async def test_a_store_that_cannot_write_answers_503_and_keeps_no_row(client, monkeypatch):
    t = await new_team(client)
    ticket = await ticket_of(client, t)

    async def disk_full(self, organization_id, key, data):
        raise StorageError("disk full")

    monkeypatch.setattr(LocalFileStorage, "put", disk_full)
    response = await upload(client, t.owner, t.org, ticket["id"])
    assert response.status_code == 503
    assert error(response)["code"] == "STORAGE_UNAVAILABLE"
    assert "disk full" not in response.text
    assert (
        await attachment_rows() == [] and await audit_entries(t.id, "ticket_attachment.added") == []
    )


# --- Permissions and tenancy ----------------------------------------------------------------

# role -> (upload, list, download, delete own file)
EXPECTED_BY_ROLE = {
    "owner": (201, 200, 200, 200),
    "admin": (201, 200, 200, 200),
    "partner": (201, 200, 200, 200),
    "manager": (201, 200, 200, 200),
    "staff": (201, 200, 200, 200),
    "billing": (201, 200, 200, 200),
    "viewer": (403, 200, 200, None),
}


@pytest.mark.parametrize("role", list(EXPECTED_BY_ROLE))
async def test_attachment_permissions_per_system_role(client, role):
    t = await new_team(client)
    actor = await t.member(role)
    ticket = await ticket_of(client, t)
    existing = await up(client, t, t.owner, ticket["id"], "pdf")
    sample = SAMPLES["png"]
    uploaded = await upload(
        client, actor, t.org, ticket["id"], sample.name, sample.data, "image/png"
    )
    listed = await client.get(files_url(t, ticket["id"]), headers=actor.headers)
    downloaded = await client.get(files_url(t, ticket["id"], existing["id"]), headers=actor.headers)
    deleted = None
    if uploaded.status_code == 201:
        own = uploaded.json()["data"]["id"]
        deleted = (
            await client.delete(files_url(t, ticket["id"], own), headers=actor.headers)
        ).status_code
    got = (uploaded.status_code, listed.status_code, downloaded.status_code, deleted)
    assert got == EXPECTED_BY_ROLE[role]


async def test_another_practices_files_are_not_found(client):
    mine = await new_team(client)
    theirs = await new_team(client, "Their Practice")
    foreign = await make_ticket(client, theirs.owner, theirs.org, "Theirs")
    secret = await up(client, theirs, theirs.owner, foreign["id"], "pdf")
    mine_ticket = await ticket_of(client, mine)
    sample = SAMPLES["png"]
    for call in (
        # Their ticket and file under my practice's path.
        client.get(files_url(mine, foreign["id"], secret["id"]), headers=mine.owner.headers),
        client.get(files_url(mine, foreign["id"]), headers=mine.owner.headers),
        client.delete(files_url(mine, foreign["id"], secret["id"]), headers=mine.owner.headers),
        upload(client, mine.owner, mine.org, foreign["id"], sample.name, sample.data, "image/png"),
        # Their file under my ticket.
        client.get(files_url(mine, mine_ticket["id"], secret["id"]), headers=mine.owner.headers),
        # Their practice's path: not a practice I belong to.
        client.get(files_url(theirs, foreign["id"], secret["id"]), headers=mine.owner.headers),
        client.get(files_url(theirs, foreign["id"]), headers=mine.owner.headers),
        client.delete(files_url(theirs, foreign["id"], secret["id"]), headers=mine.owner.headers),
        upload(
            client, mine.owner, theirs.org, foreign["id"], sample.name, sample.data, "image/png"
        ),
    ):
        assert (await call).status_code == 404
    assert len(stored_files()) == 1  # theirs, untouched
    got = await client.get(
        files_url(theirs, foreign["id"], secret["id"]), headers=theirs.owner.headers
    )
    assert got.status_code == 200 and got.content == SAMPLES["pdf"].data

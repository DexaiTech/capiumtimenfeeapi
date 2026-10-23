"""Bulk actions on staff: one action on up to 200 people, all or nothing, with the rules of the
single endpoints."""

import uuid

import pytest
from httpx import AsyncClient

from tests.clients_helpers import scalar
from tests.domain_factories import make_client, practice
from tests.factories import add_member, audit_entries, error
from tests.team_helpers import grade_id, new_staff
from tests.time_helpers import log_time, ok_data, org_url


def url(org: dict, path: str = "") -> str:
    return org_url(org, f"/staff{path}")


async def bulk(client: AsyncClient, actor, org: dict, ids: list, action: str, value=None, **extra):
    body = {"staff_ids": [str(i) for i in ids], "action": action, **extra}
    if value is not None:
        body["value"] = str(value)
    return await client.post(url(org, "/bulk"), headers=actor.headers, json=body)


async def detail(client: AsyncClient, actor, org: dict, staff_id: str) -> dict:
    return ok_data(await client.get(url(org, f"/{staff_id}"), headers=actor.headers))


async def test_set_grade_moves_only_the_people_who_differ(client: AsyncClient) -> None:
    owner, org = await practice(client)
    junior, senior = await grade_id(org, "JNR"), await grade_id(org, "SNR")
    a = await new_staff(client, owner, org, "A", grade_id=junior)
    b = await new_staff(client, owner, org, "B", grade_id=senior)
    response = await bulk(client, owner, org, [a["id"], b["id"], a["id"]], "set_grade", senior)
    assert ok_data(response) == {"action": "set_grade", "affected": 2, "changed": 1}
    assert (await detail(client, owner, org, a["id"]))["grade"]["id"] == senior
    assert len(await audit_entries(org["id"], "staff.updated")) == 1
    [summary] = await audit_entries(org["id"], "staff.bulk_changed")
    assert summary["metadata"]["action"] == "set_grade" and summary["metadata"]["count"] == 1
    assert summary["metadata"]["bulk"] is True and summary["metadata"]["codes"] == [a["code"]]


async def test_set_and_clear_the_approver(client: AsyncClient) -> None:
    owner, org = await practice(client)
    a = await new_staff(client, owner, org, "A")
    b = await new_staff(client, owner, org, "B")
    done = await bulk(client, owner, org, [a["id"], b["id"]], "set_approver", owner.id)
    assert ok_data(done)["changed"] == 2
    assert (await detail(client, owner, org, a["id"]))["approver"]["id"] == str(owner.id)
    cleared = await bulk(client, owner, org, [a["id"], b["id"]], "clear_approver")
    assert ok_data(cleared)["changed"] == 2
    assert (await detail(client, owner, org, b["id"]))["approver"] is None


async def test_deactivate_mark_as_left_and_reactivate(client: AsyncClient) -> None:
    owner, org = await practice(client)
    a = await new_staff(client, owner, org, "A")
    b = await new_staff(client, owner, org, "B")
    ids = [a["id"], b["id"]]
    assert ok_data(await bulk(client, owner, org, ids, "deactivate"))["changed"] == 2
    assert (await detail(client, owner, org, a["id"]))["status"] == "inactive"
    assert ok_data(await bulk(client, owner, org, ids, "reactivate"))["changed"] == 2
    assert (await detail(client, owner, org, a["id"]))["status"] == "active"
    left = await bulk(client, owner, org, ids, "deactivate", "left", leaving_date="2026-06-30")
    assert ok_data(left)["changed"] == 2
    gone = await detail(client, owner, org, b["id"])
    assert (gone["status"], gone["leaving_date"]) == ("left", "2026-06-30")
    # Nothing to do the second time.
    again = await bulk(client, owner, org, ids, "deactivate", "left", leaving_date="2026-06-30")
    assert ok_data(again) == {"action": "deactivate", "affected": 2, "changed": 0}


async def test_delete_removes_people_nothing_refers_to(client: AsyncClient) -> None:
    owner, org = await practice(client)
    a = await new_staff(client, owner, org, "A")
    b = await new_staff(client, owner, org, "B")
    result = ok_data(await bulk(client, owner, org, [a["id"], b["id"]], "delete"))
    assert result == {"action": "delete", "affected": 2, "changed": 2}
    for person in (a, b):
        assert (
            await client.get(url(org, f"/{person['id']}"), headers=owner.headers)
        ).status_code == 404
    assert len(await audit_entries(org["id"], "staff.deleted")) == 2


async def test_one_refusal_refuses_the_whole_batch_and_names_everyone(client: AsyncClient) -> None:
    owner, org = await practice(client)
    spare = await new_staff(client, owner, org, "Spare")
    used = await new_staff(client, owner, org, "Used")
    acme = await make_client(org)
    await log_time(client, owner, org, user_id=used["id"], client_id=acme.id)

    response = await bulk(client, owner, org, [spare["id"], used["id"], str(owner.id)], "delete")
    assert response.status_code == 409
    failure = error(response)
    assert failure["code"] == "BULK_REFUSED"
    refused = {r["staff_id"]: r["error_code"] for r in failure["details"]}
    assert refused[used["id"]] == "STAFF_IN_USE"
    assert str(owner.id) in refused  # nobody deletes themselves
    assert spare["id"] not in refused
    # Nothing was changed: the spare person is still there.
    assert (await client.get(url(org, f"/{spare['id']}"), headers=owner.headers)).status_code == 200
    assert await audit_entries(org["id"], "staff.bulk_changed") == []
    assert await audit_entries(org["id"], "staff.deleted") == []


async def test_nobody_deactivates_themselves_in_a_batch(client: AsyncClient) -> None:
    owner, org = await practice(client)
    other = await new_staff(client, owner, org, "Other")
    response = await bulk(client, owner, org, [owner.id, other["id"]], "deactivate")
    assert response.status_code == 409 and error(response)["code"] == "BULK_REFUSED"
    assert (await detail(client, owner, org, other["id"]))["status"] == "active"


async def test_an_unknown_or_foreign_id_answers_404_and_changes_nothing(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    other_owner, other_org = await practice(client)
    mine = await new_staff(client, owner, org, "Mine")
    theirs = await new_staff(client, other_owner, other_org, "Theirs")
    missing = str(uuid.uuid4())
    response = await bulk(client, owner, org, [mine["id"], theirs["id"], missing], "deactivate")
    assert response.status_code == 404
    assert sorted(error(response)["details"]["staff_ids"]) == sorted([theirs["id"], missing])
    assert (await detail(client, owner, org, mine["id"]))["status"] == "active"


@pytest.mark.parametrize(
    "body",
    [
        {"action": "set_grade"},
        {"action": "set_grade", "value": "not-an-id"},
        {"action": "set_approver", "value": "nobody"},
        {"action": "deactivate", "value": "retired"},
        {"action": "delete", "value": "x"},
        {"action": "explode"},
        {"action": "delete", "staff_ids": []},
    ],
)
async def test_the_body_is_validated(client: AsyncClient, body: dict) -> None:
    owner, org = await practice(client)
    person = await new_staff(client, owner, org, "A")
    payload = {"staff_ids": [person["id"]], **body}
    response = await client.post(url(org, "/bulk"), headers=owner.headers, json=payload)
    assert response.status_code == 422, response.text


async def test_at_most_200_people(client: AsyncClient) -> None:
    owner, org = await practice(client)
    ids = [str(uuid.uuid4()) for _ in range(201)]
    response = await client.post(
        url(org, "/bulk"), headers=owner.headers, json={"staff_ids": ids, "action": "reactivate"}
    )
    assert response.status_code == 422


async def test_bulk_needs_the_permissions_of_the_single_actions(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await new_staff(client, owner, org, "A")
    staff_member = await add_member(org["id"], "staff")
    manager = await add_member(org["id"], "manager")
    for actor in (staff_member,):
        refused = await bulk(client, actor, org, [person["id"]], "reactivate")
        assert refused.status_code == 403
    # A role that may update staff but not remove them cannot deactivate or delete in bulk.
    custom = await client.post(
        org_url(org, "/roles"),
        headers=owner.headers,
        json={"name": "Editor", "permissions": ["member.read", "member.update"]},
    )
    assert custom.status_code == 201, custom.text
    editor = await add_member(org["id"], "Editor")
    assert (await bulk(client, editor, org, [person["id"]], "reactivate")).status_code == 200
    assert (await bulk(client, editor, org, [person["id"]], "deactivate")).status_code == 403
    assert (await bulk(client, editor, org, [person["id"]], "delete")).status_code == 403
    assert manager is not None
    assert await scalar("SELECT count(*) FROM users WHERE id = :i", i=person["id"]) == 1

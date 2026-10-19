"""Staff profile fields used by time and reporting: initials and the chargeable target."""

import asyncio

import pytest
from sqlalchemy import text

from app.repositories.members import MemberRepository
from tests.factories import (
    API,
    add_member,
    audit_entries,
    create_user_with_org,
    error,
    role_id,
)
from tests.foundation_helpers import create_person, people_url


def member_url(org: dict, user) -> str:
    return f"{API}/organizations/{org['id']}/members/{user.id}"


async def patch(client, actor, org: dict, user, body: dict):
    return await client.patch(member_url(org, user), headers=actor.headers, json=body)


# --- initials ---


async def test_initials_are_upper_cased_and_audited(client):
    owner, org = await create_user_with_org(client)
    dev = await add_member(org["id"], "staff")
    response = await patch(client, owner, org, dev, {"initials": " jd "})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["initials"] == "JD"
    (entry,) = await audit_entries(org["id"], "member.updated")
    assert entry["target_id"] == str(dev.id)
    assert entry["metadata"]["changes"] == {"initials": [None, "JD"]}

    # Sending the same initials again, in any case, is not a change.
    again = await patch(client, owner, org, dev, {"initials": "jd"})
    assert again.status_code == 200 and again.json()["data"]["initials"] == "JD"
    assert len(await audit_entries(org["id"], "member.updated")) == 1


async def test_initials_are_unique_in_the_practice_case_insensitively(client, db):
    owner, org = await create_user_with_org(client)
    first = await add_member(org["id"], "staff")
    second = await add_member(org["id"], "staff")
    assert (await patch(client, owner, org, first, {"initials": "AB"})).status_code == 200

    for value in ("AB", "ab", "aB"):
        response = await patch(client, owner, org, second, {"initials": value})
        assert response.status_code == 409, value
        body = error(response)
        assert body["code"] == "INITIALS_TAKEN"
        assert body["details"][0]["field"] == "initials"
        assert body["details"][0]["code"] == "duplicate"
    stored = await db.scalar(
        text("SELECT initials FROM organization_members WHERE user_id = :u"), {"u": str(second.id)}
    )
    assert stored is None
    # Nothing else in a refused request is applied either.
    refused = await patch(client, owner, org, second, {"initials": "AB", "job_title": "Senior"})
    assert refused.status_code == 409
    shown = (await client.get(member_url(org, second), headers=owner.headers)).json()["data"]
    assert shown["job_title"] is None


async def test_initials_of_suspended_members_stay_reserved_but_removed_ones_are_freed(client):
    owner, org = await create_user_with_org(client)
    paused = await add_member(org["id"], "staff")
    leaver = await add_member(org["id"], "staff")
    newcomer = await add_member(org["id"], "staff")
    assert (await patch(client, owner, org, paused, {"initials": "PS"})).status_code == 200
    assert (await patch(client, owner, org, leaver, {"initials": "LV"})).status_code == 200
    assert (await patch(client, owner, org, paused, {"status": "suspended"})).status_code == 200
    assert (await client.delete(member_url(org, leaver), headers=owner.headers)).status_code == 200

    taken = await patch(client, owner, org, newcomer, {"initials": "ps"})
    assert taken.status_code == 409 and error(taken)["code"] == "INITIALS_TAKEN"
    freed = await patch(client, owner, org, newcomer, {"initials": "lv"})
    assert freed.status_code == 200 and freed.json()["data"]["initials"] == "LV"


async def test_the_same_initials_may_be_used_in_another_practice(client):
    owner, org = await create_user_with_org(client)
    other_owner, other = await create_user_with_org(client, "Other")
    here = await add_member(org["id"], "staff")
    there = await add_member(other["id"], "staff")
    assert (await patch(client, owner, org, here, {"initials": "XY"})).status_code == 200
    assert (await patch(client, other_owner, other, there, {"initials": "XY"})).status_code == 200


@pytest.mark.parametrize("blank", [None, ""])
async def test_initials_are_cleared_with_null_or_an_empty_string(client, blank):
    owner, org = await create_user_with_org(client)
    dev = await add_member(org["id"], "staff")
    other = await add_member(org["id"], "staff")
    assert (await patch(client, owner, org, dev, {"initials": "CL"})).status_code == 200
    cleared = await patch(client, owner, org, dev, {"initials": blank})
    assert cleared.status_code == 200 and cleared.json()["data"]["initials"] is None
    # Cleared initials are free for someone else.
    assert (await patch(client, owner, org, other, {"initials": "CL"})).status_code == 200


async def test_two_members_taking_the_same_initials_at_once_cannot_both_succeed(client, db):
    owner, org = await create_user_with_org(client)
    first = await add_member(org["id"], "staff")
    second = await add_member(org["id"], "staff")
    responses = await asyncio.gather(
        patch(client, owner, org, first, {"initials": "RC"}),
        patch(client, owner, org, second, {"initials": "rc"}),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    loser = next(r for r in responses if r.status_code == 409)
    assert error(loser)["details"][0]["field"] == "initials"
    holders = await db.scalar(
        text(
            "SELECT count(*) FROM organization_members "
            "WHERE organization_id = :org AND upper(initials) = 'RC'"
        ),
        {"org": org["id"]},
    )
    assert holders == 1


async def test_two_people_created_with_the_same_initials_at_once(client, db):
    owner, org = await create_user_with_org(client)
    staff_role = await role_id(org["id"], "staff")
    responses = await asyncio.gather(
        *(
            client.post(
                people_url(org),
                headers=owner.headers,
                json={
                    "full_name": f"Person {n}",
                    "email": f"person{n}@example.com",
                    "role_id": staff_role,
                    "initials": "PP",
                    "send_email": False,
                },
            )
            for n in range(2)
        )
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    loser = next(r for r in responses if r.status_code == 409)
    assert error(loser)["code"] == "INITIALS_TAKEN"
    # The refused person's account was not created either.
    users = await db.scalar(text("SELECT count(*) FROM users WHERE email LIKE 'person_@%'"))
    assert users == 1


async def test_the_unique_index_backstop_is_reported_as_initials_taken(client, db, monkeypatch):
    """Should the locked check ever miss, the index answers INITIALS_TAKEN, not EMAIL_TAKEN."""
    owner, org = await create_user_with_org(client)
    await create_person(client, owner, org, initials="BK")
    dev = await add_member(org["id"], "staff")

    async def looks_free(*args, **kwargs):
        return False

    monkeypatch.setattr(MemberRepository, "initials_taken", looks_free)
    created = await client.post(
        people_url(org),
        headers=owner.headers,
        json={
            "full_name": "Second",
            "email": "second@example.com",
            "role_id": await role_id(org["id"], "staff"),
            "initials": "bk",
            "send_email": False,
        },
    )
    patched = await patch(client, owner, org, dev, {"initials": "bk", "job_title": "Senior"})
    for response in (created, patched):
        assert response.status_code == 409, response.text
        body = error(response)
        assert body["code"] == "INITIALS_TAKEN"
        assert body["details"][0] == {
            "field": "initials",
            "code": "duplicate",
            "message": body["message"],
        }
    assert (
        await db.scalar(text("SELECT count(*) FROM users WHERE email = 'second@example.com'")) == 0
    )
    shown = (await client.get(member_url(org, dev), headers=owner.headers)).json()["data"]
    assert shown["initials"] is None and shown["job_title"] is None


async def test_a_person_created_with_initials_blocks_a_member_patch(client):
    owner, org = await create_user_with_org(client)
    await create_person(client, owner, org, initials="NP")
    dev = await add_member(org["id"], "staff")
    response = await patch(client, owner, org, dev, {"initials": "np"})
    assert response.status_code == 409 and error(response)["code"] == "INITIALS_TAKEN"


# --- chargeable target ---


async def test_chargeable_target_is_set_in_basis_points_and_audited(client):
    owner, org = await create_user_with_org(client)
    dev = await add_member(org["id"], "staff")
    shown = (await client.get(member_url(org, dev), headers=owner.headers)).json()["data"]
    assert shown["chargeable_target_bp"] == 7500

    for value in (6000, 0, 10_000):
        response = await patch(client, owner, org, dev, {"chargeable_target_bp": value})
        assert response.status_code == 200, value
        assert response.json()["data"]["chargeable_target_bp"] == value
    changes = [
        e["metadata"]["changes"]["chargeable_target_bp"]
        for e in await audit_entries(org["id"], "member.updated")
    ]
    assert changes == [[7500, 6000], [6000, 0], [0, 10_000]]


async def test_capacity_and_target_together_with_initials(client):
    owner, org = await create_user_with_org(client)
    dev = await add_member(org["id"], "staff")
    response = await patch(
        client,
        owner,
        org,
        dev,
        {"weekly_capacity_minutes": 1500, "chargeable_target_bp": 8000, "initials": "PT"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["weekly_capacity_minutes"], data["chargeable_target_bp"], data["initials"]) == (
        1500,
        8000,
        "PT",
    )
    (entry,) = await audit_entries(org["id"], "member.updated")
    assert set(entry["metadata"]["changes"]) == {
        "weekly_capacity_minutes",
        "chargeable_target_bp",
        "initials",
    }


# --- who may change them ---


@pytest.mark.parametrize("key", ["manager", "staff", "billing", "viewer"])
async def test_profile_fields_need_member_update_even_for_oneself(client, key):
    _, org = await create_user_with_org(client)
    actor = await add_member(org["id"], key)
    dev = await add_member(org["id"], "staff")
    for target in (actor, dev):
        for body in ({"initials": "ZZ"}, {"chargeable_target_bp": 100}):
            response = await patch(client, actor, org, target, body)
            assert response.status_code == 403, (target, body)
            assert error(response)["code"] == "PERMISSION_DENIED"
    assert await audit_entries(org["id"], "member.updated") == []


async def test_a_member_of_another_practice_is_not_found(client):
    owner, org = await create_user_with_org(client)
    _, other = await create_user_with_org(client, "Other")
    stranger = await add_member(other["id"], "staff")
    response = await patch(client, owner, org, stranger, {"initials": "ST"})
    assert response.status_code == 404 and error(response)["code"] == "RESOURCE_NOT_FOUND"
    # And the other practice's URL is not reachable at all.
    response = await patch(client, owner, other, stranger, {"initials": "ST"})
    assert response.status_code == 404

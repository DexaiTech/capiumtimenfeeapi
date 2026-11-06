"""A staff member's dated working hours (APT81, phase A): add, end, overlap, delete, validation,
permissions, audit and tenant isolation."""

import asyncio
from datetime import timedelta

from httpx import AsyncClient

from app.utils.practice_dates import today_in
from tests.clients_helpers import scalar, sql
from tests.domain_factories import practice, staff
from tests.factories import add_member, audit_entries
from tests.foundation_helpers import add_custom_role
from tests.team_helpers import new_staff
from tests.time_helpers import err, ok_data, org_url


def hours_url(org: dict, person, suffix: str = "") -> str:
    user_id = person["id"] if isinstance(person, dict) else person.id
    return org_url(org, f"/staff/{user_id}/hours{suffix}")


async def add_hours(client: AsyncClient, actor, org: dict, person, **body) -> dict:
    response = await client.post(hours_url(org, person), headers=actor.headers, json=body)
    return ok_data(response, 201)


async def listing(client: AsyncClient, actor, org: dict, person) -> list[dict]:
    return ok_data(await client.get(hours_url(org, person), headers=actor.headers))


async def test_new_hours_end_the_running_hours_the_day_before(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    assert await listing(client, owner, org, person) == []

    first = await add_hours(
        client,
        owner,
        org,
        person,
        basis="weekly",
        standard_minutes=2250,
        effective_from="2026-01-05",
    )
    assert (first["basis"], first["standard_minutes"], first["effective_to"]) == (
        "weekly",
        2250,
        None,
    )
    assert first["user_id"] == str(person.id) and first["created_by"] == str(owner.id)

    later = await add_hours(
        client,
        owner,
        org,
        person,
        basis="monthly",
        standard_minutes=9000,
        effective_from="2099-03-01",
    )
    rows = await listing(client, owner, org, person)
    # The current period first, then newest first.
    assert [(r["id"], r["status"]) for r in rows] == [
        (first["id"], "current"),
        (later["id"], "future"),
    ]
    assert rows[0]["effective_to"] == "2099-02-28"
    assert rows[1]["effective_to"] is None

    [added_one, added_two] = await audit_entries(org["id"], "staff_hours.added")
    [ended] = await audit_entries(org["id"], "staff_hours.ended")
    assert added_one["target_type"] == "member" and added_one["target_id"] == str(person.id)
    assert added_two["metadata"]["staff_hours_id"] == later["id"]
    assert added_two["metadata"]["basis"] == "monthly"
    assert ended["metadata"]["staff_hours_id"] == first["id"]
    assert ended["metadata"]["changes"] == {"effective_to": [None, "2099-02-28"]}


async def test_hours_that_cover_other_hours_are_refused(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    running = await add_hours(
        client,
        owner,
        org,
        person,
        basis="weekly",
        standard_minutes=2250,
        effective_from="2026-01-05",
    )
    # The same start day, and an earlier start: the running hours cannot end before they began.
    for start in ("2026-01-05", "2025-12-01"):
        response = await client.post(
            hours_url(org, person),
            headers=owner.headers,
            json={"basis": "weekly", "standard_minutes": 1800, "effective_from": start},
        )
        error = err(response, 409)
        assert error["code"] == "HOURS_OVERLAP", start
        assert [c["id"] for c in error["details"]["conflicts"]] == [running["id"]]
    # Two later periods: the new one would overlap both.
    await add_hours(
        client,
        owner,
        org,
        person,
        basis="weekly",
        standard_minutes=1800,
        effective_from="2026-06-01",
    )
    error = err(
        await client.post(
            hours_url(org, person),
            headers=owner.headers,
            json={"basis": "weekly", "standard_minutes": 1500, "effective_from": "2026-03-01"},
        ),
        409,
    )
    assert error["code"] == "HOURS_OVERLAP" and len(error["details"]["conflicts"]) == 2
    assert (
        await scalar("SELECT count(*) FROM staff_hours_bases WHERE user_id = :u", u=str(person.id))
        == 2
    )
    assert len(await audit_entries(org["id"], "staff_hours.added")) == 2


async def test_two_concurrent_changes_never_overlap(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)

    async def post():
        return await client.post(
            hours_url(org, person),
            headers=owner.headers,
            json={"basis": "weekly", "standard_minutes": 2250, "effective_from": "2026-01-05"},
        )

    first, second = await asyncio.gather(post(), post())
    assert sorted([first.status_code, second.status_code]) == [201, 409]
    assert (
        await scalar("SELECT count(*) FROM staff_hours_bases WHERE user_id = :u", u=str(person.id))
        == 1
    )


async def test_deleting_hours_leaves_the_others_alone(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    first = await add_hours(
        client,
        owner,
        org,
        person,
        basis="weekly",
        standard_minutes=2250,
        effective_from="2026-01-05",
    )
    second = await add_hours(
        client,
        owner,
        org,
        person,
        basis="weekly",
        standard_minutes=1800,
        effective_from="2026-06-01",
    )
    response = await client.delete(
        hours_url(org, person, f"/{second['id']}"), headers=owner.headers
    )
    assert ok_data(response) is None
    [left] = await listing(client, owner, org, person)
    assert (left["id"], left["effective_to"]) == (first["id"], "2026-05-31")
    [deleted] = await audit_entries(org["id"], "staff_hours.deleted")
    assert deleted["metadata"]["staff_hours_id"] == second["id"]
    assert deleted["metadata"]["standard_minutes"] == 1800
    # Gone now, and another person's hours are not reachable through this person.
    other = await staff(org)
    assert (
        await client.delete(hours_url(org, person, f"/{second['id']}"), headers=owner.headers)
    ).status_code == 404
    assert (
        await client.delete(hours_url(org, other, f"/{first['id']}"), headers=owner.headers)
    ).status_code == 404


async def test_invalid_hours_are_422_on_the_field(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    cases = [
        ({"basis": "daily", "standard_minutes": 450, "effective_from": "2026-01-05"}, "basis"),
        (
            {"basis": "weekly", "standard_minutes": 0, "effective_from": "2026-01-05"},
            "standard_minutes",
        ),
        (
            {"basis": "weekly", "standard_minutes": 10081, "effective_from": "2026-01-05"},
            "standard_minutes",
        ),
        (
            {"basis": "monthly", "standard_minutes": 44641, "effective_from": "2026-01-05"},
            "standard_minutes",
        ),
        ({"basis": "weekly", "standard_minutes": 2250}, "effective_from"),
        (
            {"basis": "weekly", "standard_minutes": 2250, "effective_from": "2026-01-05", "x": 1},
            "x",
        ),
    ]
    for body, field in cases:
        response = await client.post(hours_url(org, person), headers=owner.headers, json=body)
        error = err(response, 422)
        assert field in [d["field"] for d in error["details"]], (body, error)
    week = err(
        await client.post(
            hours_url(org, person),
            headers=owner.headers,
            json={"basis": "weekly", "standard_minutes": 10081, "effective_from": "2026-01-05"},
        ),
        422,
    )
    assert "168 hours" in week["details"][0]["message"]
    # A month may hold more than a week.
    monthly = await add_hours(
        client,
        owner,
        org,
        person,
        basis="monthly",
        standard_minutes=10500,
        effective_from="2026-01-01",
    )
    assert monthly["standard_minutes"] == 10500


async def test_permissions_and_other_practices(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    body = {"basis": "weekly", "standard_minutes": 2250, "effective_from": "2026-01-05"}
    # A Staff member reads (member.read) but cannot change hours (member.update).
    assert (await client.get(hours_url(org, person), headers=person.headers)).status_code == 200
    assert (
        await client.post(hours_url(org, person), headers=person.headers, json=body)
    ).status_code == 403
    row = await add_hours(client, owner, org, person, **body)
    assert (
        await client.delete(hours_url(org, person, f"/{row['id']}"), headers=person.headers)
    ).status_code == 403
    # Without member.read nothing is readable.
    await add_custom_role(org["id"], "Nobody", ["organization.read"])
    nobody = await add_member(org["id"], "Nobody")
    assert (await client.get(hours_url(org, person), headers=nobody.headers)).status_code == 403
    # An editor with member.update changes them.
    await add_custom_role(
        org["id"], "Editor", ["organization.read", "member.read", "member.update"]
    )
    editor = await add_member(org["id"], "Editor")
    assert (
        await client.delete(hours_url(org, person, f"/{row['id']}"), headers=editor.headers)
    ).status_code == 200
    # Another practice's staff member is 404, and so is an unknown one.
    _, other_org = await practice(client, "Other Practice")
    stranger = await staff(other_org)
    for target in (stranger, {"id": "00000000-0000-0000-0000-000000000001"}):
        assert (await client.get(hours_url(org, target), headers=owner.headers)).status_code == 404
        assert (
            await client.post(hours_url(org, target), headers=owner.headers, json=body)
        ).status_code == 404


async def test_hours_go_with_a_staff_record_that_nothing_else_refers_to(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    spare = await new_staff(client, owner, org, "Spare")
    await add_hours(
        client,
        owner,
        org,
        spare,
        basis="weekly",
        standard_minutes=2250,
        effective_from="2099-01-05",
    )
    detail = ok_data(await client.get(org_url(org, f"/staff/{spare['id']}"), headers=owner.headers))
    assert detail["can_delete"] is True
    deleted = await client.delete(org_url(org, f"/staff/{spare['id']}"), headers=owner.headers)
    assert deleted.status_code == 200, deleted.text
    assert await sql("SELECT 1 FROM organization_members WHERE user_id = :u", u=spare["id"]) == []
    assert await sql("SELECT 1 FROM staff_hours_bases WHERE user_id = :u", u=spare["id"]) == []


async def test_status_follows_the_practice_today(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    today = today_in(org.get("timezone") or "Europe/London")
    await add_hours(
        client,
        owner,
        org,
        person,
        basis="weekly",
        standard_minutes=2250,
        effective_from=str(today - timedelta(days=30)),
    )
    await add_hours(
        client,
        owner,
        org,
        person,
        basis="weekly",
        standard_minutes=1800,
        effective_from=str(today),
    )
    assert [r["status"] for r in await listing(client, owner, org, person)] == ["current", "ended"]

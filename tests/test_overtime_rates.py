"""Dated overtime rates of grades and staff (APT81, phase A): add, end, overlap, delete,
multiplier vs fixed validation, resolution (staff overrides grade, never both), permissions,
audit, and that time is still priced as before."""

import asyncio
import uuid

from httpx import AsyncClient

from tests.clients_helpers import scalar
from tests.domain_factories import practice, staff
from tests.factories import add_member, audit_entries
from tests.foundation_helpers import add_custom_role
from tests.team_helpers import grade_id, set_grade
from tests.time_helpers import err, ok_data, org_url

URL = "/overtime-rates"


async def add_rate(client: AsyncClient, actor, org: dict, **body) -> dict:
    payload = {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in body.items()}
    return ok_data(await client.post(org_url(org, URL), headers=actor.headers, json=payload), 201)


async def rate_error(client: AsyncClient, actor, org: dict, status: int, **body) -> dict:
    return err(await client.post(org_url(org, URL), headers=actor.headers, json=body), status)


async def resolve(client: AsyncClient, actor, org: dict, person, day: str | None = None) -> dict:
    params = {"user_id": str(person.id)} | ({"date": day} if day else {})
    return ok_data(
        await client.get(org_url(org, f"{URL}/resolve"), headers=actor.headers, params=params)
    )


async def listing(client: AsyncClient, actor, org: dict, **params) -> list[dict]:
    return ok_data(await client.get(org_url(org, URL), headers=actor.headers, params=params))


async def test_a_grade_rate_and_a_staff_rate_are_added_and_listed(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org, full_name="Sam Senior")
    snr = await grade_id(org, "SNR")
    grade_rate = await add_rate(
        client,
        owner,
        org,
        level="grade",
        grade_id=snr,
        mode="multiplier",
        multiplier_bp=15_000,
        effective_from="2026-01-01",
    )
    assert grade_rate["grade"]["id"] == snr and grade_rate["grade"]["code"] == "SNR"
    assert (grade_rate["staff"], grade_rate["mode"], grade_rate["multiplier_bp"]) == (
        None,
        "multiplier",
        15_000,
    )
    assert (grade_rate["rate"], grade_rate["status"], grade_rate["effective_to"]) == (
        None,
        "current",
        None,
    )
    staff_rate = await add_rate(
        client,
        owner,
        org,
        level="staff",
        user_id=person.id,
        mode="fixed",
        rate=3_000,
        effective_from="2099-01-01",
        effective_to="2099-12-31",
    )
    assert staff_rate["staff"]["full_name"] == "Sam Senior"
    assert (staff_rate["rate"], staff_rate["multiplier_bp"], staff_rate["status"]) == (
        3_000,
        None,
        "future",
    )

    everything = await listing(client, owner, org)
    assert [r["id"] for r in everything] == [grade_rate["id"], staff_rate["id"]]
    assert [r["id"] for r in await listing(client, owner, org, level="staff")] == [staff_rate["id"]]
    assert [r["id"] for r in await listing(client, owner, org, grade_id=snr)] == [grade_rate["id"]]
    assert [r["id"] for r in await listing(client, owner, org, user_id=str(person.id))] == [
        staff_rate["id"]
    ]
    assert [r["id"] for r in await listing(client, owner, org, status="future")] == [
        staff_rate["id"]
    ]
    assert [r["id"] for r in await listing(client, owner, org, status="current")] == [
        grade_rate["id"]
    ]
    assert await listing(client, owner, org, status="ended") == []

    [added_grade, added_staff] = await audit_entries(org["id"], "overtime_rate.added")
    assert added_grade["target_type"] == "overtime_rate"
    assert added_grade["target_id"] == grade_rate["id"]
    assert added_grade["metadata"]["multiplier_bp"] == 15_000
    assert added_staff["metadata"]["rate"] == 3_000
    assert added_staff["metadata"]["member_name"] == "Sam Senior"


async def test_overlapping_rates_of_one_scope_are_refused(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    snr = await grade_id(org, "SNR")
    first = await add_rate(
        client,
        owner,
        org,
        level="grade",
        grade_id=snr,
        mode="multiplier",
        multiplier_bp=15_000,
        effective_from="2026-01-01",
    )
    error = await rate_error(
        client,
        owner,
        org,
        409,
        level="grade",
        grade_id=snr,
        mode="fixed",
        rate=2_500,
        effective_from="2026-06-01",
    )
    assert error["code"] == "RATE_OVERLAP"
    assert [c["id"] for c in error["details"]["conflicts"]] == [first["id"]]
    # Another grade, and a person of that grade, have scopes of their own.
    await add_rate(
        client,
        owner,
        org,
        level="grade",
        grade_id=await grade_id(org, "MGR"),
        mode="multiplier",
        multiplier_bp=20_000,
        effective_from="2026-01-01",
    )
    await add_rate(
        client,
        owner,
        org,
        level="staff",
        user_id=person.id,
        mode="multiplier",
        multiplier_bp=17_500,
        effective_from="2026-01-01",
    )
    assert (
        await scalar("SELECT count(*) FROM overtime_rates WHERE organization_id = :o", o=org["id"])
        == 3
    )


async def test_two_concurrent_adds_never_overlap(client: AsyncClient) -> None:
    owner, org = await practice(client)
    snr = await grade_id(org, "SNR")
    body = {
        "level": "grade",
        "grade_id": snr,
        "mode": "multiplier",
        "multiplier_bp": 15_000,
        "effective_from": "2026-01-01",
    }
    responses = await asyncio.gather(
        *(client.post(org_url(org, URL), headers=owner.headers, json=body) for _ in range(2))
    )
    assert sorted(r.status_code for r in responses) == [201, 409]


async def test_ending_a_rate_then_adding_the_next(client: AsyncClient) -> None:
    owner, org = await practice(client)
    snr = await grade_id(org, "SNR")
    first = await add_rate(
        client,
        owner,
        org,
        level="grade",
        grade_id=snr,
        mode="multiplier",
        multiplier_bp=15_000,
        effective_from="2026-01-01",
    )
    url = org_url(org, f"{URL}/{first['id']}")
    # Before its first day: 422 on the field.
    error = err(
        await client.patch(url, headers=owner.headers, json={"effective_to": "2025-12-31"}), 422
    )
    assert error["details"][0]["field"] == "effective_to"
    # A body without the day is 422 too.
    assert (await client.patch(url, headers=owner.headers, json={})).status_code == 422

    ended = ok_data(
        await client.patch(url, headers=owner.headers, json={"effective_to": "2026-03-31"})
    )
    assert (ended["effective_to"], ended["status"]) == ("2026-03-31", "ended")
    second = await add_rate(
        client,
        owner,
        org,
        level="grade",
        grade_id=snr,
        mode="fixed",
        rate=2_800,
        effective_from="2026-04-01",
    )
    # Running the first one on again would meet the second.
    error = err(
        await client.patch(url, headers=owner.headers, json={"effective_to": "2026-04-30"}), 409
    )
    assert error["code"] == "RATE_OVERLAP"
    assert [c["id"] for c in error["details"]["conflicts"]] == [second["id"]]

    [log] = await audit_entries(org["id"], "overtime_rate.ended")
    assert log["metadata"]["changes"] == {"effective_to": [None, "2026-03-31"]}
    assert log["target_id"] == first["id"]


async def test_deleting_a_rate(client: AsyncClient) -> None:
    owner, org = await practice(client)
    snr = await grade_id(org, "SNR")
    rate = await add_rate(
        client,
        owner,
        org,
        level="grade",
        grade_id=snr,
        mode="multiplier",
        multiplier_bp=15_000,
        effective_from="2026-01-01",
    )
    url = org_url(org, f"{URL}/{rate['id']}")
    assert ok_data(await client.delete(url, headers=owner.headers)) is None
    assert await listing(client, owner, org) == []
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    assert (
        await client.patch(url, headers=owner.headers, json={"effective_to": "2026-12-31"})
    ).status_code == 404
    [log] = await audit_entries(org["id"], "overtime_rate.deleted")
    assert (log["target_id"], log["metadata"]["grade_id"]) == (rate["id"], snr)


async def test_multiplier_and_fixed_amounts_are_validated(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    snr = await grade_id(org, "SNR")
    base = {"effective_from": "2026-01-01"}
    grade = {"level": "grade", "grade_id": snr}
    cases = [
        ({**grade, "mode": "multiplier", "multiplier_bp": 9_999}, "multiplier_bp"),
        ({**grade, "mode": "multiplier", "multiplier_bp": 100_001}, "multiplier_bp"),
        ({**grade, "mode": "multiplier"}, "multiplier_bp"),
        ({**grade, "mode": "multiplier", "multiplier_bp": 15_000, "rate": 100}, "rate"),
        ({**grade, "mode": "fixed"}, "rate"),
        ({**grade, "mode": "fixed", "rate": -1}, "rate"),
        ({**grade, "mode": "fixed", "rate": 100, "multiplier_bp": 15_000}, "multiplier_bp"),
        ({**grade, "mode": "hourly", "rate": 100}, "mode"),
        ({"level": "grade", "mode": "fixed", "rate": 100}, "grade_id"),
        ({**grade, "user_id": str(person.id), "mode": "fixed", "rate": 100}, "user_id"),
        ({"level": "staff", "mode": "fixed", "rate": 100}, "user_id"),
        (
            {
                "level": "staff",
                "user_id": str(person.id),
                "grade_id": snr,
                "mode": "fixed",
                "rate": 1,
            },
            "grade_id",
        ),
        ({**grade, "mode": "fixed", "rate": 100, "effective_to": "2025-12-31"}, "effective_to"),
        # Ids of nothing in this practice.
        ({"level": "grade", "grade_id": str(uuid.uuid4()), "mode": "fixed", "rate": 1}, "grade_id"),
        ({"level": "staff", "user_id": str(uuid.uuid4()), "mode": "fixed", "rate": 1}, "user_id"),
    ]
    for body, field in cases:
        error = await rate_error(client, owner, org, 422, **base, **body)
        assert field in [d["field"] for d in error["details"]], (body, error)
    # The bounds themselves are accepted, and a fixed amount may be 0.
    await add_rate(
        client,
        owner,
        org,
        **base,
        **grade,
        mode="multiplier",
        multiplier_bp=10_000,
        effective_to="2026-01-31",
    )
    await add_rate(
        client,
        owner,
        org,
        level="grade",
        grade_id=snr,
        mode="multiplier",
        multiplier_bp=100_000,
        effective_from="2026-02-01",
        effective_to="2026-02-28",
    )
    await add_rate(
        client,
        owner,
        org,
        level="grade",
        grade_id=snr,
        mode="fixed",
        rate=0,
        effective_from="2026-03-01",
    )
    assert len(await listing(client, owner, org)) == 3


async def test_a_person_who_has_left_gets_no_new_rate(client: AsyncClient) -> None:
    owner, org = await practice(client)
    gone = await add_member(org["id"], "staff", status="left")
    error = await rate_error(
        client,
        owner,
        org,
        409,
        level="staff",
        user_id=str(gone.id),
        mode="fixed",
        rate=1_000,
        effective_from="2026-01-01",
    )
    assert error["code"] == "MEMBER_REMOVED"


async def test_resolve_staff_overrides_grade_and_never_stacks(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    await set_grade(org, person, "SNR")
    ungraded = await staff(org)
    snr = await grade_id(org, "SNR")

    # Nothing set: no rate.
    nothing = await resolve(client, owner, org, person, "2026-05-01")
    assert (nothing["source"], nothing["rate"], nothing["grade_id"]) == (None, None, snr)
    assert nothing["on_date"] == "2026-05-01" and nothing["user_id"] == str(person.id)

    grade_rate = await add_rate(
        client,
        owner,
        org,
        level="grade",
        grade_id=snr,
        mode="multiplier",
        multiplier_bp=15_000,
        effective_from="2026-01-01",
    )
    staff_rate = await add_rate(
        client,
        owner,
        org,
        level="staff",
        user_id=person.id,
        mode="fixed",
        rate=3_000,
        effective_from="2026-06-01",
        effective_to="2026-08-31",
    )
    # Before the staff rate starts: the grade's.
    before = await resolve(client, owner, org, person, "2026-05-31")
    assert (before["source"], before["rate"]["id"]) == ("grade", grade_rate["id"])
    # While it runs: the staff rate alone, never both.
    during = await resolve(client, owner, org, person, "2026-06-01")
    assert (during["source"], during["rate"]["id"], during["rate"]["rate"]) == (
        "staff",
        staff_rate["id"],
        3_000,
    )
    assert during["rate"]["multiplier_bp"] is None
    # After it ended: the grade's again.
    after = await resolve(client, owner, org, person, "2026-09-01")
    assert (after["source"], after["rate"]["id"]) == ("grade", grade_rate["id"])
    # Before any rate: none.
    early = await resolve(client, owner, org, person, "2025-12-31")
    assert (early["source"], early["rate"]) == (None, None)
    # A future rate applies from its day; today is the default day.
    future = await add_rate(
        client,
        owner,
        org,
        level="staff",
        user_id=person.id,
        mode="multiplier",
        multiplier_bp=20_000,
        effective_from="2099-01-01",
    )
    assert (await resolve(client, owner, org, person, "2099-01-01"))["rate"]["id"] == future["id"]
    today = await resolve(client, owner, org, person)
    assert today["source"] == "grade" and today["on_date"]
    # Someone without a grade or a rate of their own has none.
    bare = await resolve(client, owner, org, ungraded, "2026-06-01")
    assert (bare["source"], bare["rate"], bare["grade_id"]) == (None, None, None)
    # An unknown person is 404.
    response = await client.get(
        org_url(org, f"{URL}/resolve"),
        headers=owner.headers,
        params={"user_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404


async def test_permissions_and_cost_masking(client: AsyncClient) -> None:
    owner, org = await practice(client)
    snr = await grade_id(org, "SNR")
    person = await staff(org)
    body = {
        "level": "grade",
        "grade_id": snr,
        "mode": "fixed",
        "rate": 1_234,
        "effective_from": "2026-01-01",
    }
    # Staff have neither rate.read nor rate.manage.
    assert (await client.get(org_url(org, URL), headers=person.headers)).status_code == 403
    assert (
        await client.get(
            org_url(org, f"{URL}/resolve"),
            headers=person.headers,
            params={"user_id": str(person.id)},
        )
    ).status_code == 403
    assert (
        await client.post(org_url(org, URL), headers=person.headers, json=body)
    ).status_code == 403
    rate = await add_rate(client, owner, org, **body)
    rate_url = org_url(org, f"{URL}/{rate['id']}")
    assert (
        await client.patch(rate_url, headers=person.headers, json={"effective_to": "2026-12-31"})
    ).status_code == 403
    assert (await client.delete(rate_url, headers=person.headers)).status_code == 403

    # A manager reads (rate.read) but cannot change them (no rate.manage).
    manager = await add_member(org["id"], "manager")
    assert len(await listing(client, manager, org)) == 1
    assert (
        await client.post(org_url(org, URL), headers=manager.headers, json=body)
    ).status_code == 403

    # rate.manage without rate.read changes rates but never sees a fixed amount.
    await add_custom_role(
        org["id"], "Rate editor", ["organization.read", "member.read", "rate.manage"]
    )
    editor = await add_member(org["id"], "Rate editor")
    assert (await client.get(org_url(org, URL), headers=editor.headers)).status_code == 403
    ended = await client.patch(
        rate_url, headers=editor.headers, json={"effective_to": "2026-12-31"}
    )
    assert ended.status_code == 200 and ended.json()["data"]["rate"] is None
    assert ":1234" not in ended.text

    # Another practice's rate is 404.
    other_owner, other_org = await practice(client, "Other Practice")
    assert (
        await client.delete(org_url(other_org, f"{URL}/{rate['id']}"), headers=other_owner.headers)
    ).status_code == 404
    assert await listing(client, other_owner, other_org) == []


async def test_a_grade_with_an_overtime_rate_is_deactivated_not_deleted(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    grade = ok_data(
        await client.post(
            org_url(org, "/grades"), headers=owner.headers, json={"code": "OT", "name": "Overtime"}
        ),
        201,
    )
    await add_rate(
        client,
        owner,
        org,
        level="grade",
        grade_id=grade["id"],
        mode="multiplier",
        multiplier_bp=15_000,
        effective_from="2026-01-01",
    )
    url = org_url(org, f"/grades/{grade['id']}")
    assert ok_data(await client.get(url, headers=owner.headers))["can_delete"] is False
    outcome = ok_data(await client.delete(url, headers=owner.headers))
    assert outcome == {"outcome": "deactivated", "id": grade["id"]}

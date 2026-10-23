"""Staff: records, validation, listing, lifecycle, deletion, the administrator guard, duplicate
emails and codes, permissions, rate masking and tenant isolation. Logins are in
``test_staff_login.py``."""

import pytest
from httpx import AsyncClient

from tests.clients_helpers import count_queries, scalar, sql
from tests.domain_factories import make_client, practice, staff
from tests.factories import add_member, audit_entries, error, role_id
from tests.foundation_helpers import create_person
from tests.team_helpers import grade_id, new_rate, new_staff, set_grade
from tests.time_helpers import log_time, ok_data, org_url, set_practice


def staff_url(org: dict, path: str = "") -> str:
    return org_url(org, f"/staff{path}")


async def detail(client: AsyncClient, actor, org: dict, staff_id: str) -> dict:
    return ok_data(await client.get(staff_url(org, f"/{staff_id}"), headers=actor.headers))


async def listing(client: AsyncClient, actor, org: dict, **params) -> list[dict]:
    return ok_data(await client.get(staff_url(org), headers=actor.headers, params=params))


# --- Create and read ----------------------------------------------------------------------------


async def test_the_founder_is_staff_and_new_staff_have_no_login(client: AsyncClient, db) -> None:
    owner, org = await practice(client)
    [founder] = await listing(client, owner, org)
    assert founder["id"] == str(owner.id) and founder["code"] == "S001"
    assert (founder["has_login"], founder["login_status"], founder["login_email"]) == (
        True,
        "active",
        owner.email,
    )
    assert founder["role"]["system_key"] == "owner" and founder["grade"] is None
    assert (founder["status"], founder["is_selectable"], founder["email"]) == (
        "active",
        True,
        owner.email,
    )
    assert (
        founder["working_minutes_per_day"],
        founder["working_days"],
        founder["weekly_capacity_minutes"],
    ) == (450, [1, 2, 3, 4, 5], 2250)

    created = await new_staff(client, owner, org, "Sam Staff")
    assert created["code"] == "S002" and created["full_name"] == "Sam Staff"
    assert (
        created["has_login"],
        created["login_status"],
        created["login_email"],
        created["invited_at"],
    ) == (False, "none", None, None)
    assert (created["status"], created["is_selectable"]) == ("active", True)
    assert created["grade"] == {"id": await grade_id(org), "code": "SNR", "name": "Senior"}
    assert created["approver"] is None and created["email"] is None and created["initials"] is None
    assert created["role"]["system_key"] == "staff"
    assert (
        created["working_minutes_per_day"],
        created["working_days"],
        created["weekly_capacity_minutes"],
    ) == (450, [1, 2, 3, 4, 5], 2250)
    assert (created["chargeable_target_bp"], created["start_date"], created["leaving_date"]) == (
        7_500,
        "2026-01-05",
        None,
    )
    assert created["usage"] == [] and created["can_delete"] is True

    # The record stands on an inactive placeholder account nobody can sign in with.
    user = (
        await sql(
            "SELECT email, is_active, email_verified_at FROM users WHERE id = :i", i=created["id"]
        )
    )[0]
    assert user["is_active"] is False and user["email_verified_at"] is None
    assert user["email"] == f"staff+{created['id']}@no-login.invalid"
    [log] = await audit_entries(org["id"], "staff.created")
    assert log["target_id"] == created["id"] and log["metadata"]["staff_code"] == "S002"
    assert (
        ok_data(await client.get(staff_url(org, f"/{created['id']}"), headers=owner.headers))["id"]
        == created["id"]
    )
    assert len(await listing(client, owner, org)) == 2


async def test_create_with_every_field(client: AsyncClient) -> None:
    owner, org = await practice(client)
    manager_role = await role_id(org["id"], "manager")
    created = await new_staff(
        client,
        owner,
        org,
        "Ada Lovelace",
        code="abc-9",
        email="Ada@Example.com",
        approver_id=owner.id,
        job_title="Senior accountant",
        initials="al",
        working_minutes_per_day=420,
        working_days=[4, 1, 2, 3],
        chargeable_target_bp=6_000,
        leaving_date="2027-01-01",
        role_id=manager_role,
        start_date="2026-02-02",
    )
    assert created["code"] == "ABC-9" and created["email"] == "Ada@example.com"
    assert created["approver"] == {"id": str(owner.id), "full_name": owner.full_name}
    assert (created["job_title"], created["initials"]) == ("Senior accountant", "AL")
    assert (
        created["working_minutes_per_day"],
        created["working_days"],
        created["weekly_capacity_minutes"],
    ) == (420, [1, 2, 3, 4], 1_680)
    assert (
        created["chargeable_target_bp"],
        created["leaving_date"],
        created["role"]["system_key"],
    ) == (6_000, "2027-01-01", "manager")


async def test_defaults_follow_the_practice_and_the_grade(client: AsyncClient) -> None:
    owner, org = await practice(client)
    await set_practice(org, working_days=[1, 2, 3], minutes_per_day=480)
    manager = await new_staff(
        client, owner, org, "Mia Manager", grade_id=await grade_id(org, "MGR")
    )
    assert (
        manager["working_minutes_per_day"],
        manager["working_days"],
        manager["weekly_capacity_minutes"],
    ) == (480, [1, 2, 3], 1_440)
    assert manager["chargeable_target_bp"] == 7_000  # the grade's target
    await sql(
        "UPDATE grades SET default_chargeable_target_bp = NULL WHERE organization_id = :o AND code = 'MGR'",
        o=org["id"],
    )
    assert (await new_staff(client, owner, org, "Nia", grade_id=await grade_id(org, "MGR")))[
        "chargeable_target_bp"
    ] == 7_500


@pytest.mark.parametrize(
    "body",
    [
        {"grade_id": "G", "start_date": "2026-01-05"},
        {"full_name": "X", "start_date": "2026-01-05"},
        {"full_name": "X", "grade_id": "G"},
        {"full_name": "", "grade_id": "G", "start_date": "2026-01-05"},
        {"full_name": "X" * 121, "grade_id": "G", "start_date": "2026-01-05"},
        {"full_name": "X", "grade_id": "not-a-uuid", "start_date": "2026-01-05"},
        {
            "full_name": "X",
            "grade_id": "00000000-0000-0000-0000-000000000000",
            "start_date": "2026-01-05",
        },
        {
            "full_name": "X",
            "grade_id": "G",
            "start_date": "2026-01-05",
            "leaving_date": "2026-01-04",
        },
        {"full_name": "X", "grade_id": "G", "start_date": "2026-01-05", "working_days": []},
        {"full_name": "X", "grade_id": "G", "start_date": "2026-01-05", "working_days": [0, 1]},
        {"full_name": "X", "grade_id": "G", "start_date": "2026-01-05", "working_days": [8]},
        {"full_name": "X", "grade_id": "G", "start_date": "2026-01-05", "working_days": [1, 1]},
        {
            "full_name": "X",
            "grade_id": "G",
            "start_date": "2026-01-05",
            "working_minutes_per_day": 0,
        },
        {
            "full_name": "X",
            "grade_id": "G",
            "start_date": "2026-01-05",
            "working_minutes_per_day": 1_441,
        },
        {
            "full_name": "X",
            "grade_id": "G",
            "start_date": "2026-01-05",
            "chargeable_target_bp": 10_001,
        },
        {"full_name": "X", "grade_id": "G", "start_date": "2026-01-05", "email": "not-an-email"},
        {"full_name": "X", "grade_id": "G", "start_date": "2026-01-05", "initials": "TOOLONGX"},
        {"full_name": "X", "grade_id": "G", "start_date": "2026-01-05", "initials": "a b"},
        {"full_name": "X", "grade_id": "G", "start_date": "2026-01-05", "code": "has space"},
        {"full_name": "X", "grade_id": "G", "start_date": "2026-01-05", "code": "C" * 21},
        {"full_name": "X", "grade_id": "G", "start_date": "2026-01-05", "colour": "red"},
        {
            "full_name": "X",
            "grade_id": "G",
            "start_date": "2026-01-05",
            "approver_id": "00000000-0000-0000-0000-000000000000",
        },
        {
            "full_name": "X",
            "grade_id": "G",
            "start_date": "2026-01-05",
            "role_id": "00000000-0000-0000-0000-000000000000",
        },
    ],
)
async def test_staff_validation(client: AsyncClient, body: dict) -> None:
    owner, org = await practice(client)
    body = {k: (await grade_id(org) if v == "G" else v) for k, v in body.items()}
    response = await client.post(staff_url(org), headers=owner.headers, json=body)
    assert response.status_code == 422, response.text
    assert len(await listing(client, owner, org)) == 1  # only the founder


async def test_codes_are_unique_and_generated_codes_skip_taken_ones(client: AsyncClient) -> None:
    owner, org = await practice(client)
    first = await new_staff(client, owner, org, "First", code="s002")  # takes the next number
    assert first["code"] == "S002"
    generated = await new_staff(client, owner, org, "Second")
    assert generated["code"] == "S003"  # S002 was skipped
    for code in ("S002", "s002", "S003"):
        response = await client.post(
            staff_url(org),
            headers=owner.headers,
            json={
                "full_name": "Dup",
                "grade_id": await grade_id(org),
                "start_date": "2026-01-05",
                "code": code,
            },
        )
        assert response.status_code == 409 and error(response)["code"] == "STAFF_CODE_TAKEN"
        assert error(response)["details"][0]["field"] == "code"
    other = await new_staff(client, owner, org, "Other", code="OTHER")
    response = await client.patch(
        staff_url(org, f"/{other['id']}"), headers=owner.headers, json={"code": "s003"}
    )
    assert response.status_code == 409 and error(response)["code"] == "STAFF_CODE_TAKEN"
    assert (
        await client.patch(
            staff_url(org, f"/{other['id']}"), headers=owner.headers, json={"code": "other"}
        )
    ).status_code == 200
    # Codes are per practice.
    other_owner, other_org = await practice(client, "Other & Co")
    assert (await new_staff(client, other_owner, other_org, "Same", code="S003"))["code"] == "S003"


async def test_the_staff_sequence_drives_codes_and_can_be_changed(client: AsyncClient) -> None:
    owner, org = await practice(client)
    response = await client.patch(
        org_url(org, "/sequences/staff"),
        headers=owner.headers,
        json={"prefix": "emp-", "padding": 4, "next_number": 40},
    )
    assert response.status_code == 200 and response.json()["data"]["preview"] == "emp-0040"
    assert (await new_staff(client, owner, org, "A"))["code"] == "EMP-0040"
    assert (await new_staff(client, owner, org, "B"))["code"] == "EMP-0041"
    for body in ({"prefix": "A/B"}, {"prefix": "X" * 12, "padding": 10}):
        assert (
            await client.patch(org_url(org, "/sequences/staff"), headers=owner.headers, json=body)
        ).status_code == 422
    # A prefix that breaks the code format is reported when a code is taken, not stored.
    await sql(
        "UPDATE document_sequences SET prefix = 'A/', padding = 3 WHERE organization_id = :o AND kind = 'staff'",
        o=org["id"],
    )
    response = await client.post(
        staff_url(org),
        headers=owner.headers,
        json={"full_name": "Z", "grade_id": await grade_id(org), "start_date": "2026-01-05"},
    )
    assert response.status_code == 409 and error(response)["code"] == "SEQUENCE_INVALID"
    assert (await new_staff(client, owner, org, "Manual", code="MANUAL1"))["code"] == "MANUAL1"


async def test_no_duplicate_email_among_active_staff(client: AsyncClient) -> None:
    owner, org = await practice(client)
    first = await new_staff(client, owner, org, "First", email="first@example.com")

    async def create(email: str, status: int):
        response = await client.post(
            staff_url(org),
            headers=owner.headers,
            json={
                "full_name": "Dup",
                "grade_id": await grade_id(org),
                "start_date": "2026-01-05",
                "email": email,
            },
        )
        assert response.status_code == status, response.text
        return response

    for email in (
        "first@example.com",
        "FIRST@Example.com",
        owner.email.upper(),
    ):  # a work email and a login email
        response = await create(email, 409)
        assert (
            error(response)["code"] == "STAFF_EMAIL_TAKEN"
            and error(response)["details"][0]["field"] == "email"
        )
    second = await new_staff(client, owner, org, "Second", email="second@example.com")
    response = await client.patch(
        staff_url(org, f"/{second['id']}"),
        headers=owner.headers,
        json={"email": "First@example.com"},
    )
    assert response.status_code == 409 and error(response)["code"] == "STAFF_EMAIL_TAKEN"
    # A person created the V1 way cannot take a staff member's work email either.
    response = await client.post(
        org_url(org, "/people"),
        headers=owner.headers,
        json={
            "full_name": "P",
            "email": "first@example.com",
            "role_id": await role_id(org["id"], "staff"),
        },
    )
    assert response.status_code == 409 and error(response)["code"] == "STAFF_EMAIL_TAKEN"

    # Leaving frees the address; coming back needs it to still be free.
    await client.post(
        staff_url(org, f"/{first['id']}/deactivate"), headers=owner.headers, json={"status": "left"}
    )
    replacement = await new_staff(client, owner, org, "Replacement", email="first@example.com")
    response = await client.post(
        staff_url(org, f"/{first['id']}/reactivate"), headers=owner.headers
    )
    assert response.status_code == 409 and error(response)["code"] == "STAFF_EMAIL_TAKEN"
    await client.post(
        staff_url(org, f"/{replacement['id']}/deactivate"),
        headers=owner.headers,
        json={"status": "inactive"},
    )
    assert (
        await client.post(staff_url(org, f"/{first['id']}/reactivate"), headers=owner.headers)
    ).status_code == 200
    # Other practices are separate, and an empty email never collides.
    other_owner, other_org = await practice(client, "Other & Co")
    await new_staff(client, other_owner, other_org, "Same", email="first@example.com")
    await new_staff(client, owner, org, "No email 1")
    await new_staff(client, owner, org, "No email 2")
    response = await client.patch(
        staff_url(org, f"/{second['id']}"), headers=owner.headers, json={"email": ""}
    )
    assert ok_data(response)["email"] is None


# --- Update ---------------------------------------------------------------------------------------


async def test_update_staff_details(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await new_staff(
        client, owner, org, "Sam Staff", email="sam@example.com", job_title="Clerk", initials="SS"
    )
    manager = await add_member(org["id"], "manager")
    url = staff_url(org, f"/{person['id']}")
    updated = ok_data(
        await client.patch(
            url,
            headers=owner.headers,
            json={
                "full_name": "Samantha Staff",
                "email": "samantha@example.com",
                "grade_id": await grade_id(org, "MGR"),
                "approver_id": str(manager.id),
                "job_title": "Manager",
                "initials": "sam",
                "working_minutes_per_day": 360,
                "working_days": [1, 3, 5],
                "chargeable_target_bp": 6_500,
                "start_date": "2026-02-02",
                "leaving_date": "2030-01-01",
            },
        )
    )
    assert updated["full_name"] == "Samantha Staff" and updated["grade"]["code"] == "MGR"
    assert updated["approver"]["id"] == str(manager.id) and updated["initials"] == "SAM"
    assert (
        updated["working_minutes_per_day"],
        updated["working_days"],
        updated["weekly_capacity_minutes"],
    ) == (360, [1, 3, 5], 1_080)
    assert (updated["chargeable_target_bp"], updated["start_date"], updated["leaving_date"]) == (
        6_500,
        "2026-02-02",
        "2030-01-01",
    )
    # Only the days or only the hours change: capacity follows.
    assert (
        ok_data(await client.patch(url, headers=owner.headers, json={"working_days": [1, 2]}))[
            "weekly_capacity_minutes"
        ]
        == 720
    )
    assert (
        ok_data(
            await client.patch(url, headers=owner.headers, json={"working_minutes_per_day": 400})
        )["weekly_capacity_minutes"]
        == 800
    )
    # Null clears what can be cleared; nothing in the body changes nothing.
    cleared = ok_data(
        await client.patch(
            url,
            headers=owner.headers,
            json={
                "email": None,
                "approver_id": None,
                "job_title": "",
                "initials": None,
                "start_date": None,
                "leaving_date": None,
            },
        )
    )
    assert all(
        cleared[k] is None
        for k in ("email", "approver", "job_title", "initials", "start_date", "leaving_date")
    )
    assert ok_data(await client.patch(url, headers=owner.headers, json={})) == cleared
    changes = [e["metadata"]["changes"] for e in await audit_entries(org["id"], "staff.updated")]
    assert "grade_id" in changes[0] and "weekly_capacity_minutes" in changes[0]
    assert changes[0]["full_name"] == ["Sam Staff", "Samantha Staff"]
    assert len(changes) == 4  # the empty patch is not logged


async def test_update_validation_and_conflicts(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await new_staff(client, owner, org, "Sam Staff", start_date="2026-03-01")
    other = await new_staff(client, owner, org, "Other", initials="OT")
    lead = ok_data(
        await client.post(
            org_url(org, "/grades"),
            headers=owner.headers,
            json={"code": "OLD", "name": "Old", "is_active": False},
        ),
        201,
    )
    url = staff_url(org, f"/{person['id']}")
    for body in (
        {"full_name": None},
        {"code": None},
        {"grade_id": None},
        {"working_days": None},
        {"working_minutes_per_day": None},
        {"chargeable_target_bp": None},
        {"leaving_date": "2026-02-28"},
        {"working_days": [9]},
        {"grade_id": "00000000-0000-0000-0000-000000000000"},
        {"grade_id": lead["id"]},
        {"approver_id": str(person.get("id"))},
        {"approver_id": "00000000-0000-0000-0000-000000000000"},
    ):
        response = await client.patch(url, headers=owner.headers, json=body)
        assert response.status_code == 422, (body, response.text)
    response = await client.patch(url, headers=owner.headers, json={"initials": "ot"})
    assert response.status_code == 409 and error(response)["code"] == "INITIALS_TAKEN"
    assert other["initials"] == "OT"
    # Unknown people, and people of other practices.
    _, other_org = await practice(client, "Other & Co")
    for path in ("/00000000-0000-0000-0000-000000000000", f"/{await _foreign(client, other_org)}"):
        assert (
            await client.patch(staff_url(org, path), headers=owner.headers, json={"job_title": "x"})
        ).status_code == 404
        assert (await client.get(staff_url(org, path), headers=owner.headers)).status_code == 404


async def _foreign(client: AsyncClient, org: dict) -> str:
    owner = (await sql("SELECT created_by FROM organizations WHERE id = :o", o=org["id"]))[0][
        "created_by"
    ]
    return str(owner)


async def test_approvers_must_be_active_staff_who_can_sign_in(client: AsyncClient) -> None:
    owner, org = await practice(client)
    nologin = await new_staff(client, owner, org, "No Login")
    gone = await add_member(org["id"], "manager", status="suspended")
    manager = await add_member(org["id"], "manager")
    for approver in (nologin["id"], str(gone.id)):
        response = await client.post(
            staff_url(org),
            headers=owner.headers,
            json={
                "full_name": "X",
                "grade_id": await grade_id(org),
                "start_date": "2026-01-05",
                "approver_id": approver,
            },
        )
        assert (
            response.status_code == 422 and error(response)["details"][0]["field"] == "approver_id"
        )
    ok = await new_staff(client, owner, org, "Reports to manager", approver_id=str(manager.id))
    assert ok["approver"]["id"] == str(manager.id)
    # Nobody approves themselves.
    response = await client.patch(
        staff_url(org, f"/{manager.id}"),
        headers=owner.headers,
        json={"approver_id": str(manager.id)},
    )
    assert response.status_code == 422
    assert [s["id"] for s in await listing(client, owner, org, approver_id=str(manager.id))] == [
        ok["id"]
    ]


# --- Listing --------------------------------------------------------------------------------------


async def _team(client: AsyncClient):
    owner, org = await practice(client)
    people = {}
    for name, code, grade, start, email in (
        ("Alice", "C3", "PTR", "2026-03-01", "alice@example.com"),
        ("bob", "A1", "JNR", "2026-01-01", "bob@example.com"),
        ("Carol", "B2", "MGR", "2026-02-01", "carol@example.com"),
        ("dave", "E5", "SNR", "2026-05-01", "dave@example.com"),
        ("Eve", "D4", "SNR", "2026-04-01", "eve@example.com"),
    ):
        people[name] = await new_staff(
            client,
            owner,
            org,
            name,
            code=code,
            grade_id=await grade_id(org, grade),
            start_date=start,
            email=email,
        )
    await set_grade(org, people["Eve"]["id"], None)
    await client.patch(
        staff_url(org, f"/{people['Eve']['id']}"),
        headers=owner.headers,
        json={"start_date": None, "leaving_date": None},
    )
    await client.post(
        staff_url(org, f"/{people['bob']['id']}/deactivate"),
        headers=owner.headers,
        json={"status": "inactive"},
    )
    return owner, org, people


async def test_search_filters_and_sorts(client: AsyncClient) -> None:
    owner, org, people = await _team(client)
    ids = {name: p["id"] for name, p in people.items()}

    async def names(**params) -> list[str]:
        found = await listing(client, owner, org, has_login="false", **params)
        reverse = {v: k for k, v in ids.items()}
        return [reverse[s["id"]] for s in found]

    assert await names() == ["Alice", "bob", "Carol", "dave", "Eve"]  # case-insensitive by name
    assert await names(order="desc") == ["Eve", "dave", "Carol", "bob", "Alice"]
    assert await names(sort="code") == ["bob", "Carol", "Alice", "Eve", "dave"]
    assert await names(sort="email") == ["Alice", "bob", "Carol", "dave", "Eve"]
    assert await names(sort="grade") == [
        "Alice",
        "Carol",
        "dave",
        "bob",
        "Eve",
    ]  # Partner first; no grade last
    assert await names(sort="start_date") == [
        "Eve",
        "bob",
        "Carol",
        "Alice",
        "dave",
    ]  # unknown first
    assert (await names(sort="status"))[-1] == "bob"  # active before inactive
    # Search: name, code, work email, login email.
    assert await names(q="ALI") == ["Alice"] and await names(q="b2") == ["Carol"]
    assert await names(q="dave@") == ["dave"]
    assert [s["id"] for s in await listing(client, owner, org, q=owner.email[:8])] == [
        str(owner.id)
    ]
    assert await names(q="%") == [] and await names(q="no such person") == []
    # Filters.
    assert await names(status="inactive") == ["bob"] and await names(
        status=["active", "inactive"]
    ) == ["Alice", "bob", "Carol", "dave", "Eve"]
    assert await names(status="left") == []
    assert await names(grade_id=await grade_id(org, "SNR")) == ["dave"]
    assert len(await listing(client, owner, org, has_login="true")) == 1
    assert await names(selectable="true") == ["Alice", "Carol", "dave", "Eve"]
    assert await names(selectable="false") == ["bob"]
    assert [
        s["id"]
        for s in await listing(client, owner, org, role_id=await role_id(org["id"], "owner"))
    ] == [str(owner.id)]
    for bad in (
        {"sort": "nope"},
        {"order": "sideways"},
        {"status": "gone"},
        {"limit": 0},
        {"limit": 101},
    ):
        assert (
            await client.get(staff_url(org), headers=owner.headers, params=bad)
        ).status_code == 422, bad


async def test_a_page_costs_the_same_number_of_queries_whatever_its_size(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)

    async def add(count: int) -> None:
        for number in range(count):
            person = await new_staff(client, owner, org, f"Person {number}")
            await new_rate(
                client,
                owner,
                org,
                kind="charge",
                user_id=person["id"],
                rate=9_000 + number,
                effective_from="2026-01-01",
            )

    await add(2)
    with count_queries() as few:
        await listing(client, owner, org)
    await add(8)
    with count_queries() as many:
        found = await listing(client, owner, org, limit=100)
    assert len(found) == 11 and len(many) == len(few), (len(few), len(many))
    assert sum(1 for s in found if s["charge_rate_source"] == "staff") == 10


@pytest.mark.parametrize(
    "sort", ["name", "code", "email", "grade", "status", "start_date", "leaving_date"]
)
@pytest.mark.parametrize("order", ["asc", "desc"])
async def test_pagination_walks_every_staff_member_exactly_once(
    client: AsyncClient, sort: str, order: str
) -> None:
    owner, org, _ = await _team(client)
    whole = [s["id"] for s in await listing(client, owner, org, sort=sort, order=order, limit=100)]
    assert len(whole) == 6
    seen, cursor = [], None
    while True:
        params = {"limit": 2, "sort": sort, "order": order} | ({"cursor": cursor} if cursor else {})
        response = await client.get(staff_url(org), headers=owner.headers, params=params)
        seen += [s["id"] for s in response.json()["data"]]
        cursor = response.json()["meta"].get("next_cursor")
        if not cursor:
            break
    assert seen == whole
    assert (
        await client.get(staff_url(org), headers=owner.headers, params={"cursor": "garbage"})
    ).status_code in (400, 422)


# --- Lifecycle ------------------------------------------------------------------------------------


async def test_deactivate_mark_as_left_and_reactivate(client: AsyncClient) -> None:
    owner, org = await practice(client)
    manager = await add_member(org["id"], "manager")
    reports = await new_staff(client, owner, org, "Reports", approver_id=str(manager.id))
    person = await new_staff(client, owner, org, "Sam Staff", initials="SS")
    url = staff_url(org, f"/{person['id']}")

    paused = ok_data(await client.post(f"{url}/deactivate", headers=owner.headers, json={}))
    assert (paused["status"], paused["is_selectable"], paused["leaving_date"]) == (
        "inactive",
        False,
        None,
    )
    assert (
        ok_data(
            await client.post(
                f"{url}/deactivate", headers=owner.headers, json={"status": "inactive"}
            )
        )["status"]
        == "inactive"
    )  # idempotent
    assert len(await audit_entries(org["id"], "staff.deactivated")) == 1
    left = ok_data(
        await client.post(
            f"{url}/deactivate",
            headers=owner.headers,
            json={"status": "left", "leaving_date": "2026-06-30"},
        )
    )
    assert (left["status"], left["leaving_date"], left["is_selectable"]) == (
        "left",
        "2026-06-30",
        False,
    )
    response = await client.post(
        f"{url}/deactivate", headers=owner.headers, json={"status": "inactive"}
    )
    assert response.status_code == 409 and error(response)["code"] == "STAFF_LEFT"
    assert [s["id"] for s in await listing(client, owner, org, status="left")] == [person["id"]]
    assert person["id"] not in [s["id"] for s in await listing(client, owner, org)]
    # The record is still there with its history.
    assert (await detail(client, owner, org, person["id"]))["initials"] == "SS"

    back = ok_data(await client.post(f"{url}/reactivate", headers=owner.headers))
    assert (back["status"], back["leaving_date"], back["is_selectable"]) == ("active", None, True)
    assert (
        ok_data(await client.post(f"{url}/reactivate", headers=owner.headers))["status"] == "active"
    )  # idempotent
    assert [
        e["metadata"]["new"]["status"] for e in await audit_entries(org["id"], "staff.left")
    ] == ["left"]
    assert len(await audit_entries(org["id"], "staff.reactivated")) == 1

    # Leaving clears who they approve: the people they approved lose their approver.
    await client.post(
        staff_url(org, f"/{manager.id}/deactivate"), headers=owner.headers, json={"status": "left"}
    )
    assert (await detail(client, owner, org, reports["id"]))["approver"] is None
    log = (await audit_entries(org["id"], "staff.left"))[-1]["metadata"]
    assert log["approver_links_cleared"] == 1
    # The default leaving date is today.
    moved = ok_data(
        await client.post(
            staff_url(org, f"/{reports['id']}/deactivate"),
            headers=owner.headers,
            json={"status": "left"},
        )
    )
    assert moved["leaving_date"] is not None
    # A leaving date before the start date is refused.
    other = await new_staff(client, owner, org, "Other", start_date="2026-05-01")
    response = await client.post(
        staff_url(org, f"/{other['id']}/deactivate"),
        headers=owner.headers,
        json={"status": "left", "leaving_date": "2026-04-01"},
    )
    assert (
        response.status_code == 422
        and response.json()["error"]["details"][0]["field"] == "leaving_date"
    )
    for body in ({"status": "gone"}, {"leaving_date": "x"}):
        assert (
            await client.post(f"{url}/deactivate", headers=owner.headers, json=body)
        ).status_code == 422


async def test_a_person_who_left_cannot_sign_in_and_is_not_offered_for_time(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    manager = await add_member(org["id"], "manager")
    assert (await client.get(org_url(org, "/staff"), headers=manager.headers)).status_code == 200
    gone = ok_data(
        await client.post(
            staff_url(org, f"/{manager.id}/deactivate"),
            headers=owner.headers,
            json={"status": "left"},
        )
    )
    assert gone["status"] == "left"
    # Their access to the practice ends at once: they are signed out everywhere.
    assert (await client.get(org_url(org, "/staff"), headers=manager.headers)).status_code == 401
    response = await client.post(
        org_url(org, "/time-entries"),
        headers=owner.headers,
        json={"user_id": str(manager.id), "entry_date": "2026-09-07", "minutes": 30},
    )
    assert response.status_code == 422
    # The V1 member list reports them as removed people would be.
    assert str(manager.id) not in [
        m["user_id"]
        for m in ok_data(await client.get(org_url(org, "/members"), headers=owner.headers))
    ]
    left = ok_data(
        await client.get(org_url(org, "/members"), headers=owner.headers, params={"status": "left"})
    )
    assert [m["user_id"] for m in left] == [str(manager.id)] and left[0]["status"] == "left"


async def test_time_cannot_be_logged_after_the_leaving_date(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await new_staff(client, owner, org, "Leaver", leaving_date="2026-09-01")
    acme = await make_client(org)
    before = await log_time(
        client, owner, org, user_id=person["id"], client_id=acme.id, entry_date="2026-08-31"
    )
    assert before["user_id"] == person["id"]
    refused = await log_time(
        client,
        owner,
        org,
        status=422,
        user_id=person["id"],
        client_id=acme.id,
        entry_date="2026-09-07",
    )
    assert (
        refused["details"][0]["field"] == "entry_date"
        and refused["details"][0]["code"] == "after_leaving_date"
    )
    on_the_day = await log_time(
        client, owner, org, user_id=person["id"], client_id=acme.id, entry_date="2026-09-01"
    )
    # Moving an entry past the leaving date is refused as well.
    moved = await client.patch(
        org_url(org, f"/time-entries/{on_the_day['id']}"),
        headers=owner.headers,
        json={"entry_date": "2026-09-02"},
    )
    assert moved.status_code == 422 and error(moved)["details"][0]["code"] == "after_leaving_date"
    # Extending the leaving date lifts it.
    await client.patch(
        staff_url(org, f"/{person['id']}"), headers=owner.headers, json={"leaving_date": None}
    )
    assert (
        await log_time(
            client, owner, org, user_id=person["id"], client_id=acme.id, entry_date="2026-09-07"
        )
    )["entry_date"] == "2026-09-07"


async def test_delete_only_staff_that_nothing_refers_to(client: AsyncClient) -> None:
    owner, org = await practice(client)
    spare = await new_staff(client, owner, org, "Spare")
    used = await new_staff(client, owner, org, "Used")
    acme = await make_client(org)
    await log_time(client, owner, org, user_id=used["id"], client_id=acme.id)

    detail_used = await detail(client, owner, org, used["id"])
    assert detail_used["can_delete"] is False
    assert {u["kind"] for u in detail_used["usage"]} >= {"time_entries", "timesheets"}
    assert all(u["count"] >= 1 and u["label"] for u in detail_used["usage"])
    response = await client.delete(staff_url(org, f"/{used['id']}"), headers=owner.headers)
    assert response.status_code == 409 and error(response)["code"] == "STAFF_IN_USE"
    assert {u["kind"] for u in error(response)["details"]["usage"]} >= {"time_entries"}
    # ... but they can be deactivated and their history stays.
    assert (
        ok_data(
            await client.post(
                staff_url(org, f"/{used['id']}/deactivate"), headers=owner.headers, json={}
            )
        )["status"]
        == "inactive"
    )
    assert (
        len(
            ok_data(
                await client.get(
                    org_url(org, "/time-entries"),
                    headers=owner.headers,
                    params={"user_id": used["id"]},
                )
            )
        )
        == 1
    )

    assert (
        await client.delete(staff_url(org, f"/{spare['id']}"), headers=owner.headers)
    ).status_code == 200
    assert (
        await client.get(staff_url(org, f"/{spare['id']}"), headers=owner.headers)
    ).status_code == 404
    assert (
        await scalar("SELECT count(*) FROM users WHERE id = :i", i=spare["id"]) == 0
    )  # the placeholder account too
    assert len(await audit_entries(org["id"], "staff.deleted")) == 1
    # A staff member with a rate is in use as well.
    ruled = await new_staff(client, owner, org, "Ruled")
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=ruled["id"],
        rate=1_000,
        effective_from="2026-01-01",
    )
    assert (
        await client.delete(staff_url(org, f"/{ruled['id']}"), headers=owner.headers)
    ).status_code == 409
    # Deleting someone who approves others clears their approver links.
    approver = await add_member(org["id"], "manager")
    reports = await new_staff(client, owner, org, "Reports", approver_id=str(approver.id))
    assert (
        await client.delete(staff_url(org, f"/{approver.id}"), headers=owner.headers)
    ).status_code == 200
    assert (await detail(client, owner, org, reports["id"]))["approver"] is None
    assert (
        await client.get(staff_url(org, f"/{approver.id}"), headers=owner.headers)
    ).status_code == 404


async def test_nobody_deactivates_or_deletes_themselves(client: AsyncClient) -> None:
    _owner, org = await practice(client)
    admin = await add_member(org["id"], "admin")
    for method, path, body in (
        ("post", "/deactivate", {}),
        ("delete", "", None),
        ("post", "/unlink-login", None),
    ):
        response = await client.request(
            method.upper(),
            staff_url(org, f"/{admin.id}{path}"),
            headers=admin.headers,
            **({"json": body} if body is not None else {}),
        )
        assert response.status_code == 403, path


async def test_the_practice_always_keeps_an_administrator(client: AsyncClient) -> None:
    owner, org = await practice(client)
    admin = await add_member(org["id"], "admin")
    # A second owner who cannot sign in (suspended) does not count.
    await add_member(org["id"], "owner", status="suspended")
    attempts = [
        client.post(
            staff_url(org, f"/{owner.id}/deactivate"),
            headers=admin.headers,
            json={"status": "left"},
        ),
        client.delete(staff_url(org, f"/{owner.id}"), headers=admin.headers),
        client.post(staff_url(org, f"/{owner.id}/unlink-login"), headers=admin.headers),
    ]
    for response in [await attempt for attempt in attempts]:
        assert response.status_code == 409 and error(response)["details"] == {"code": "last_owner"}
    assert (await detail(client, owner, org, str(owner.id)))["status"] == "active"
    # With a second active owner, one can be deactivated by an owner, never by an admin.
    second = await add_member(org["id"], "owner")
    response = await client.post(
        staff_url(org, f"/{second.id}/deactivate"), headers=admin.headers, json={}
    )
    assert response.status_code == 403
    assert (
        await client.post(
            staff_url(org, f"/{second.id}/deactivate"), headers=owner.headers, json={}
        )
    ).status_code == 200
    # ... and then the guard bites again.
    response = await client.post(
        staff_url(org, f"/{owner.id}/deactivate"), headers=admin.headers, json={}
    )
    assert response.status_code == 409 and error(response)["details"] == {"code": "last_owner"}
    # An owner without a login is not an administrator either.
    placeholder = await new_staff(
        client, owner, org, "Owner without login", role_id=await role_id(org["id"], "owner")
    )
    assert placeholder["role"]["system_key"] == "owner" and placeholder["has_login"] is False
    response = await client.post(
        staff_url(org, f"/{owner.id}/deactivate"), headers=admin.headers, json={}
    )
    assert response.status_code == 409


async def test_two_owners_cannot_deactivate_each_other_at_the_same_time(
    client: AsyncClient, db
) -> None:
    import asyncio

    owner, org = await practice(client)
    second = await add_member(org["id"], "owner")
    responses = await asyncio.gather(
        client.post(staff_url(org, f"/{second.id}/deactivate"), headers=owner.headers, json={}),
        client.post(staff_url(org, f"/{owner.id}/deactivate"), headers=second.headers, json={}),
    )
    assert sorted(r.status_code for r in responses)[0] == 200
    # The caller of the slower request was deactivated by the first: no longer a member.
    assert sorted(r.status_code for r in responses)[1] in (404, 409)
    active_owners = await scalar(
        "SELECT count(*) FROM organization_members m JOIN roles r ON r.id = m.role_id "
        "WHERE m.organization_id = :o AND m.status = 'active' AND r.system_key = 'owner'",
        o=org["id"],
    )
    assert active_owners == 1


# --- Rates, V1, permissions, tenants ------------------------------------------------------------------


async def test_staff_show_their_standard_rates_with_the_level_that_prices_them(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    await set_practice(org, default_charge_rate=2_000)
    person = await new_staff(client, owner, org, "Sam Staff")
    assert (
        person["charge_rate"],
        person["charge_rate_source"],
        person["cost_rate"],
        person["cost_rate_source"],
    ) == (2_000, "default", 0, "none")
    await sql(
        "UPDATE grades SET default_charge_rate = 3000, default_cost_rate = 1000 WHERE organization_id = :o AND code = 'SNR'",
        o=org["id"],
    )
    shown = await detail(client, owner, org, person["id"])
    assert (
        shown["charge_rate"],
        shown["charge_rate_source"],
        shown["cost_rate"],
        shown["cost_rate_source"],
    ) == (3_000, "grade", 1_000, "grade")
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person["id"],
        rate=4_000,
        effective_from="2026-01-01",
    )
    await new_rate(
        client,
        owner,
        org,
        kind="cost",
        user_id=person["id"],
        rate=1_500,
        effective_from="2026-01-01",
    )
    shown = (await listing(client, owner, org, q="Sam"))[0]
    assert (
        shown["charge_rate"],
        shown["charge_rate_source"],
        shown["cost_rate"],
        shown["cost_rate_source"],
    ) == (4_000, "staff", 1_500, "staff")
    # A rule that starts in the future does not apply yet.
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person["id"],
        rate=9_000,
        effective_from="2099-01-01",
        close_previous=True,
    )
    assert (await detail(client, owner, org, person["id"]))["charge_rate"] == 4_000


async def test_a_new_person_can_get_their_rates_in_the_same_request(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await new_staff(client, owner, org, "Sam Staff", cost_rate=4_800, charge_rate=8_500)
    assert (
        person["charge_rate"],
        person["charge_rate_source"],
        person["cost_rate"],
        person["cost_rate_source"],
    ) == (8_500, "staff", 4_800, "staff")
    rules = await sql(
        "SELECT kind, level, rate, effective_from::text AS starts, effective_to FROM rate_rules "
        "WHERE user_id = :u ORDER BY kind",
        u=person["id"],
    )
    assert rules == [
        {
            "kind": "charge",
            "level": "staff",
            "rate": 8_500,
            "starts": "2026-01-05",
            "effective_to": None,
        },
        {
            "kind": "cost",
            "level": "staff",
            "rate": 4_800,
            "starts": "2026-01-05",
            "effective_to": None,
        },
    ]
    assert len(await audit_entries(org["id"], "rate.created")) == 2
    [created] = await audit_entries(org["id"], "staff.created")
    assert created["metadata"]["staff_code"] == person["code"]
    history = await sql(
        "SELECT action, source FROM rate_changes WHERE user_id = :u", u=person["id"]
    )
    assert {(row["action"], row["source"]) for row in history} == {("created", "staff_rates")}
    assert len(history) == 2


async def test_the_rates_of_a_new_person_start_when_asked_and_either_may_be_left_out(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    await set_practice(org, default_charge_rate=2_000)
    only_cost = await new_staff(client, owner, org, "Cost Only", cost_rate=3_000)
    assert only_cost["cost_rate"] == 3_000 and only_cost["charge_rate_source"] == "default"
    later = await new_staff(
        client, owner, org, "Starts Later", charge_rate=9_000, rate_effective_from="2099-01-01"
    )
    # Not in force yet: the practice default still prices them.
    assert (later["charge_rate"], later["charge_rate_source"]) == (2_000, "default")
    [rule] = await sql(
        "SELECT effective_from::text AS starts FROM rate_rules WHERE user_id = :u", u=later["id"]
    )
    assert rule["starts"] == "2099-01-01"
    plain = await new_staff(client, owner, org, "No Rates")
    assert await sql("SELECT 1 FROM rate_rules WHERE user_id = :u", u=plain["id"]) == []


async def test_rates_with_a_new_person_need_rate_manage_and_nothing_is_created_without_it(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    created = await client.post(
        org_url(org, "/roles"),
        headers=owner.headers,
        json={"name": "Recruiter", "permissions": ["member.read", "member.invite"]},
    )
    assert created.status_code == 201, created.text
    recruiter = await add_member(org["id"], "Recruiter")
    body = {"full_name": "Sam Staff", "grade_id": await grade_id(org), "start_date": "2026-01-05"}
    refused = await client.post(
        staff_url(org), headers=recruiter.headers, json={**body, "cost_rate": 4_800}
    )
    assert refused.status_code == 403, refused.text
    names = await sql("SELECT 1 FROM users WHERE full_name = 'Sam Staff'")
    assert names == []
    # Without rates the same person may add staff.
    ok = await client.post(staff_url(org), headers=recruiter.headers, json=body)
    assert ok.status_code == 201, ok.text


@pytest.mark.parametrize("field", ["cost_rate", "charge_rate"])
async def test_the_rates_of_a_new_person_are_validated(client: AsyncClient, field: str) -> None:
    owner, org = await practice(client)
    body = {"full_name": "Sam Staff", "grade_id": await grade_id(org), "start_date": "2026-01-05"}
    for bad in (-1, "free", 10**13):
        response = await client.post(
            staff_url(org), headers=owner.headers, json={**body, field: bad}
        )
        assert response.status_code == 422, (field, bad)
        assert error(response)["details"][0]["field"] == field


async def test_rates_are_hidden_from_people_without_rate_read(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await new_staff(client, owner, org, "Sam Staff")
    await new_rate(
        client,
        owner,
        org,
        kind="cost",
        user_id=person["id"],
        rate=1_500,
        effective_from="2026-01-01",
    )
    manager, plain, viewer = (
        await staff(org, "manager"),
        await staff(org, "staff"),
        await staff(org, "viewer"),
    )
    seen = await detail(client, manager, org, person["id"])
    assert seen["cost_rate"] == 1_500
    for actor in (plain, viewer):
        for shown in (
            await detail(client, actor, org, person["id"]),
            *await listing(client, actor, org),
        ):
            assert all(
                shown[k] is None
                for k in ("charge_rate", "charge_rate_source", "cost_rate", "cost_rate_source")
            )
    response = await client.get(staff_url(org, f"/{person['id']}"), headers=plain.headers)
    assert "1500" not in response.text


async def test_non_login_staff_appear_in_the_v1_member_list(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await new_staff(client, owner, org, "Sam Staff", email="sam@example.com")
    members = ok_data(await client.get(org_url(org, "/members"), headers=owner.headers))
    row = next(m for m in members if m["user_id"] == person["id"])
    assert (row["email"], row["full_name"], row["status"]) == (
        "sam@example.com",
        "Sam Staff",
        "active",
    )
    assert row["role"]["system_key"] == "staff"
    await client.post(staff_url(org, f"/{person['id']}/deactivate"), headers=owner.headers, json={})
    members = ok_data(await client.get(org_url(org, "/members"), headers=owner.headers))
    assert next(m for m in members if m["user_id"] == person["id"])["status"] == "inactive"
    detail_v1 = ok_data(
        await client.get(org_url(org, f"/members/{person['id']}"), headers=owner.headers)
    )
    assert detail_v1["email"] == "sam@example.com"
    # People created the V1 way are staff with a code and their email as work email.
    created = await create_person(client, owner, org, email="newbie@example.com")
    staff_row = await detail(client, owner, org, created["member"]["user_id"])
    assert (
        staff_row["code"].startswith("S")
        and staff_row["email"] == "newbie@example.com"
        and staff_row["has_login"] is True
    )


@pytest.mark.parametrize(
    ("role", "can_read", "can_invite", "can_update", "can_remove"),
    [
        ("owner", True, True, True, True),
        ("admin", True, True, True, True),
        ("partner", True, True, True, True),
        ("manager", True, False, False, False),
        ("staff", True, False, False, False),
        ("billing", True, False, False, False),
        ("viewer", True, False, False, False),
    ],
)
async def test_permissions_per_role(
    client: AsyncClient,
    role: str,
    can_read: bool,
    can_invite: bool,
    can_update: bool,
    can_remove: bool,
) -> None:
    owner, org = await practice(client)
    actor = owner if role == "owner" else await staff(org, role)
    target = await new_staff(client, owner, org, "Target")
    spare = await new_staff(client, owner, org, "Spare")
    assert (await client.get(staff_url(org), headers=actor.headers)).status_code == (
        200 if can_read else 403
    )
    assert (
        await client.get(staff_url(org, f"/{target['id']}"), headers=actor.headers)
    ).status_code == (200 if can_read else 403)
    body = {"full_name": "New", "grade_id": await grade_id(org), "start_date": "2026-01-05"}
    created = await client.post(staff_url(org), headers=actor.headers, json=body)
    assert created.status_code == (201 if can_invite else 403)
    patched = await client.patch(
        staff_url(org, f"/{target['id']}"), headers=actor.headers, json={"job_title": "x"}
    )
    assert patched.status_code == (200 if can_update else 403)
    reactivated = await client.post(
        staff_url(org, f"/{target['id']}/reactivate"), headers=actor.headers
    )
    assert reactivated.status_code == (200 if can_update else 403)
    invited = await client.post(
        staff_url(org, f"/{target['id']}/invite"),
        headers=actor.headers,
        json={"email": "t@example.com"},
    )
    assert invited.status_code == (200 if can_invite else 403), invited.text
    linked = await client.post(
        staff_url(org, f"/{spare['id']}/link-login"),
        headers=actor.headers,
        json={"mode": "create_account", "email": "s@example.com"},
    )
    assert linked.status_code == (200 if can_invite else 403), linked.text
    unlinked = await client.post(
        staff_url(org, f"/{spare['id']}/unlink-login"), headers=actor.headers
    )
    assert unlinked.status_code == (200 if can_remove else 403), unlinked.text
    deactivated = await client.post(
        staff_url(org, f"/{target['id']}/deactivate"), headers=actor.headers, json={}
    )
    assert deactivated.status_code == (200 if can_remove else 403)
    deleted = await client.delete(staff_url(org, f"/{target['id']}"), headers=actor.headers)
    assert deleted.status_code in ((409, 200) if can_remove else (403,))


async def test_other_practices_staff_are_invisible(client: AsyncClient) -> None:
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other & Co")
    theirs = await new_staff(client, other_owner, other, "Their Staff", email="same@example.com")
    ours = await new_staff(
        client, owner, org, "Our Staff", email="same@example.com"
    )  # emails are per practice
    assert [s["id"] for s in await listing(client, owner, org)].count(theirs["id"]) == 0
    for method, path, body in (
        ("GET", "", None),
        ("PATCH", "", {"job_title": "x"}),
        ("DELETE", "", None),
        ("POST", "/deactivate", {}),
        ("POST", "/reactivate", None),
        ("POST", "/invite", {"email": "x@example.com"}),
        ("POST", "/link-login", {"mode": "create_account", "email": "x@example.com"}),
        ("POST", "/unlink-login", None),
    ):
        kwargs = {"json": body} if body is not None else {}
        response = await client.request(
            method, staff_url(org, f"/{theirs['id']}{path}"), headers=owner.headers, **kwargs
        )
        assert response.status_code == 404, (method, path, response.status_code)
    # Not a member of the practice at all: 404 for the whole area.
    assert (await client.get(staff_url(other), headers=owner.headers)).status_code == 404
    assert ours["id"] != theirs["id"] and ours["email"] == theirs["email"]
    # Approvers and roles of another practice cannot be named.
    foreign_role = await role_id(other["id"], "staff")
    response = await client.post(
        staff_url(org),
        headers=owner.headers,
        json={
            "full_name": "X",
            "grade_id": await grade_id(org),
            "start_date": "2026-01-05",
            "role_id": foreign_role,
        },
    )
    assert response.status_code == 422
    response = await client.post(
        staff_url(org),
        headers=owner.headers,
        json={
            "full_name": "X",
            "grade_id": await grade_id(org),
            "start_date": "2026-01-05",
            "approver_id": str(other_owner.id),
        },
    )
    assert response.status_code == 422

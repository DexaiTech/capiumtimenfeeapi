"""Staff grades: the defaults a practice starts with, validation, default rates and their
history, deletion, permissions, cost masking and tenant isolation."""

import pytest
from httpx import AsyncClient

from tests.clients_helpers import sql
from tests.domain_factories import make_client, practice, staff
from tests.factories import add_member, audit_entries
from tests.foundation_helpers import add_custom_role
from tests.team_helpers import grade_id, new_rate, new_staff
from tests.time_helpers import entry_row, log_time, ok_data, org_url

DEFAULTS = [
    ("PTR", "Partner", 5_000),
    ("MGR", "Manager", 7_000),
    ("SNR", "Senior", 7_500),
    ("JNR", "Junior", 8_000),
]


async def create_grade(client: AsyncClient, actor, org: dict, status: int = 201, **body):
    body = {"code": "LEAD", "name": "Team Lead"} | body
    response = await client.post(org_url(org, "/grades"), headers=actor.headers, json=body)
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else response.json()["error"]


async def test_a_new_practice_has_the_default_grades(client: AsyncClient) -> None:
    owner, org = await practice(client)
    grades = ok_data(await client.get(org_url(org, "/grades"), headers=owner.headers))
    assert [(g["code"], g["name"], g["default_chargeable_target_bp"]) for g in grades] == DEFAULTS
    assert all(g["default_cost_rate"] is None and g["default_charge_rate"] is None for g in grades)
    assert all(g["is_active"] and g["staff_count"] == 0 and g["can_delete"] for g in grades)
    assert [g["sort_order"] for g in grades] == [0, 1, 2, 3]
    assert {"id", "code", "name", "description", "created_at", "updated_at"} <= set(grades[0])


async def test_create_read_update_and_clear_a_grade(client: AsyncClient) -> None:
    owner, org = await practice(client)
    created = await create_grade(
        client,
        owner,
        org,
        code="lead",
        description="  Runs a team  ",
        default_cost_rate=2_500,
        default_charge_rate=9_500,
        default_chargeable_target_bp=6_500,
        sort_order=5,
    )
    assert created["code"] == "LEAD" and created["description"] == "Runs a team"
    assert (
        created["default_cost_rate"],
        created["default_charge_rate"],
        created["default_chargeable_target_bp"],
    ) == (2_500, 9_500, 6_500)
    assert (
        created["is_active"] is True and created["staff_count"] == 0 and created["sort_order"] == 5
    )
    url = org_url(org, f"/grades/{created['id']}")
    assert ok_data(await client.get(url, headers=owner.headers)) == created

    updated = ok_data(
        await client.patch(
            url,
            headers=owner.headers,
            json={"name": "Lead", "default_charge_rate": 10_000, "sort_order": 1},
        )
    )
    assert (updated["name"], updated["default_charge_rate"], updated["sort_order"]) == (
        "Lead",
        10_000,
        1,
    )
    # null clears the optional values; a blank description is cleared too.
    cleared = ok_data(
        await client.patch(
            url,
            headers=owner.headers,
            json={
                "description": "",
                "default_cost_rate": None,
                "default_charge_rate": None,
                "default_chargeable_target_bp": None,
            },
        )
    )
    assert cleared["description"] is None and cleared["default_cost_rate"] is None
    assert (
        cleared["default_charge_rate"] is None and cleared["default_chargeable_target_bp"] is None
    )
    # An empty body changes nothing.
    assert ok_data(await client.patch(url, headers=owner.headers, json={})) == cleared

    [made] = await audit_entries(org["id"], "grade.created")
    assert made["target_id"] == created["id"] and made["metadata"]["code"] == "LEAD"
    changes = [e["metadata"]["changes"] for e in await audit_entries(org["id"], "grade.updated")]
    assert changes[0]["name"] == ["Team Lead", "Lead"] and "default_charge_rate" in changes[1]


async def test_default_rates_are_in_the_rate_history_and_price_new_time_only(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    grade = await create_grade(
        client, owner, org, default_cost_rate=2_000, default_charge_rate=8_000
    )
    person = await staff(org)
    acme = await make_client(org)
    await sql(
        "UPDATE organization_members SET grade_id = :g WHERE organization_id = :o AND user_id = :u",
        g=grade["id"],
        o=org["id"],
        u=str(person.id),
    )

    before = await log_time(client, person, org, client_id=acme.id, minutes=60)
    assert (before["charge_rate"], before["rate_source"]) == (8_000, "grade")
    assert (await entry_row(before["id"])).cost_rate == 2_000

    url = org_url(org, f"/grades/{grade['id']}")
    await client.patch(url, headers=owner.headers, json={"default_charge_rate": 9_000})
    await client.patch(url, headers=owner.headers, json={"default_cost_rate": None})
    after = await log_time(
        client, person, org, client_id=acme.id, minutes=60, entry_date="2026-09-08"
    )
    assert (after["charge_rate"], (await entry_row(after["id"])).cost_rate) == (9_000, 0)
    assert (await entry_row(before["id"])).charge_rate == 8_000  # recorded time is not rewritten

    history = ok_data(
        await client.get(
            org_url(org, "/rates/history"), headers=owner.headers, params={"grade_id": grade["id"]}
        )
    )
    # Newest first: the cost default cleared, the charge default changed, then the creation.
    assert [(c["kind"], c["action"]) for c in history][:2] == [
        ("cost", "deleted"),
        ("charge", "updated"),
    ]
    assert sorted((c["kind"], c["action"]) for c in history) == [
        ("charge", "created"),
        ("charge", "updated"),
        ("cost", "created"),
        ("cost", "deleted"),
    ]
    assert all(
        c["source"] == "grade_default" and c["rule_id"] is None and c["level"] == "grade"
        for c in history
    )
    updated = next(c for c in history if c["action"] == "updated")
    assert updated["old"] == {"rate": 8_000, "default": True} and updated["new"] == {
        "rate": 9_000,
        "default": True,
    }
    assert next(c for c in history if c["action"] == "deleted")["new"] is None


async def test_name_and_code_are_unique_ignoring_case(client: AsyncClient) -> None:
    owner, org = await practice(client)
    assert (await create_grade(client, owner, org, 409, code="ptr", name="Something else"))[
        "code"
    ] == "GRADE_CODE_TAKEN"
    assert (await create_grade(client, owner, org, 409, code="NEW1", name="partner"))[
        "code"
    ] == "GRADE_NAME_TAKEN"
    lead = await create_grade(client, owner, org)
    url = org_url(org, f"/grades/{lead['id']}")
    for body, code in (
        ({"code": "mgr"}, "GRADE_CODE_TAKEN"),
        ({"name": "MANAGER"}, "GRADE_NAME_TAKEN"),
    ):
        response = await client.patch(url, headers=owner.headers, json=body)
        assert response.status_code == 409 and response.json()["error"]["code"] == code
        assert response.json()["error"]["details"][0]["code"] == "duplicate"
    # Keeping its own name and code is fine.
    assert (
        await client.patch(url, headers=owner.headers, json={"name": "team lead", "code": "Lead"})
    ).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"code": "", "name": "X"},
        {"code": "HAS SPACE", "name": "X"},
        {"code": "WAYTOOLONGCODE1", "name": "X"},
        {"code": "OK", "name": ""},
        {"code": "OK", "name": "N" * 81},
        {"code": "OK", "name": "X", "default_cost_rate": -1},
        {"code": "OK", "name": "X", "default_charge_rate": 10**13},
        {"code": "OK", "name": "X", "default_chargeable_target_bp": 10_001},
        {"code": "OK", "name": "X", "description": "d" * 501},
        {"code": "OK", "name": "X", "colour": "red"},
        {"name": "X"},
    ],
)
async def test_grade_validation(client: AsyncClient, body: dict) -> None:
    owner, org = await practice(client)
    response = await client.post(org_url(org, "/grades"), headers=owner.headers, json=body)
    assert response.status_code == 422, response.text
    grades = ok_data(await client.get(org_url(org, "/grades"), headers=owner.headers))
    assert len(grades) == 4


async def test_patch_cannot_null_required_fields(client: AsyncClient) -> None:
    owner, org = await practice(client)
    url = org_url(org, f"/grades/{await grade_id(org, 'PTR')}")
    for field in ("code", "name", "sort_order", "is_active"):
        response = await client.patch(url, headers=owner.headers, json={field: None})
        assert response.status_code == 422, field


async def test_list_filters_sort_and_pagination(client: AsyncClient) -> None:
    owner, org = await practice(client)
    await create_grade(
        client,
        owner,
        org,
        code="ZED",
        name="Zed grade",
        description="Spare",
        sort_order=9,
        is_active=False,
    )

    async def listing(**params):
        return ok_data(
            await client.get(org_url(org, "/grades"), headers=owner.headers, params=params)
        )

    assert [g["code"] for g in await listing()] == ["PTR", "MGR", "SNR", "JNR", "ZED"]
    assert [g["code"] for g in await listing(is_active="false")] == ["ZED"]
    assert len(await listing(is_active="true")) == 4
    assert [g["code"] for g in await listing(q="spare")] == ["ZED"]
    assert [g["code"] for g in await listing(q="man")] == ["MGR"]
    assert [g["name"] for g in await listing(sort="name")] == [
        "Junior",
        "Manager",
        "Partner",
        "Senior",
        "Zed grade",
    ]
    assert [g["code"] for g in await listing(sort="code", order="desc")] == [
        "ZED",
        "SNR",
        "PTR",
        "MGR",
        "JNR",
    ]
    seen, cursor = [], None
    while True:
        params = {"limit": 2, "sort": "name"} | ({"cursor": cursor} if cursor else {})
        response = await client.get(org_url(org, "/grades"), headers=owner.headers, params=params)
        seen += [g["code"] for g in response.json()["data"]]
        cursor = response.json()["meta"].get("next_cursor")
        if not cursor:
            break
    assert seen == ["JNR", "MGR", "PTR", "SNR", "ZED"]


async def test_deactivate_activate_and_delete_outcomes(client: AsyncClient) -> None:
    owner, org = await practice(client)
    unused = await create_grade(client, owner, org, code="TMP", name="Temp")
    gone = ok_data(
        await client.delete(org_url(org, f"/grades/{unused['id']}"), headers=owner.headers)
    )
    assert gone == {"outcome": "deleted", "id": unused["id"]}
    assert (
        await client.get(org_url(org, f"/grades/{unused['id']}"), headers=owner.headers)
    ).status_code == 404

    # A grade that a staff member holds, or a rate names, is deactivated instead.
    held, ruled = (
        await create_grade(client, owner, org, code="HELD", name="Held"),
        await create_grade(client, owner, org, code="RULED", name="Ruled"),
    )
    await new_staff(client, owner, org, grade_id=held["id"])
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        grade_id=ruled["id"],
        rate=1_000,
        effective_from="2026-01-01",
    )
    for grade in (held, ruled):
        url = org_url(org, f"/grades/{grade['id']}")
        before = ok_data(await client.get(url, headers=owner.headers))
        assert before["can_delete"] is False
        outcome = ok_data(await client.delete(url, headers=owner.headers))
        assert outcome == {"outcome": "deactivated", "id": grade["id"]}
        after = ok_data(await client.get(url, headers=owner.headers))
        assert after["is_active"] is False
        # Deleting again changes nothing.
        assert ok_data(await client.delete(url, headers=owner.headers))["outcome"] == "deactivated"
    assert len(await audit_entries(org["id"], "grade.deactivated")) == 2

    url = org_url(org, f"/grades/{held['id']}")
    assert ok_data(await client.post(f"{url}/activate", headers=owner.headers))["is_active"] is True
    assert (
        ok_data(await client.post(f"{url}/deactivate", headers=owner.headers))["is_active"] is False
    )
    assert (
        await client.post(org_url(org, f"/grades/{unused['id']}/activate"), headers=owner.headers)
    ).status_code == 404


async def test_an_inactive_grade_cannot_be_given_but_those_who_hold_it_keep_it(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    lead = await create_grade(client, owner, org)
    holder = await new_staff(client, owner, org, "Holder", grade_id=lead["id"])
    await client.post(org_url(org, f"/grades/{lead['id']}/deactivate"), headers=owner.headers)
    response = await client.post(
        org_url(org, "/staff"),
        headers=owner.headers,
        json={"full_name": "New", "grade_id": lead["id"], "start_date": "2026-01-05"},
    )
    assert (
        response.status_code == 422
        and response.json()["error"]["details"][0]["field"] == "grade_id"
    )
    other = await new_staff(client, owner, org, "Other")
    response = await client.patch(
        org_url(org, f"/staff/{other['id']}"), headers=owner.headers, json={"grade_id": lead["id"]}
    )
    assert response.status_code == 422
    kept = ok_data(await client.get(org_url(org, f"/staff/{holder['id']}"), headers=owner.headers))
    assert kept["grade"]["id"] == lead["id"]


async def test_staff_count_ignores_people_who_left(client: AsyncClient) -> None:
    owner, org = await practice(client)
    snr = await grade_id(org, "SNR")
    first, second = (
        await new_staff(client, owner, org, "First"),
        await new_staff(client, owner, org, "Second"),
    )
    assert (
        ok_data(await client.get(org_url(org, f"/grades/{snr}"), headers=owner.headers))[
            "staff_count"
        ]
        == 2
    )
    await client.post(
        org_url(org, f"/staff/{second['id']}/deactivate"),
        headers=owner.headers,
        json={"status": "left"},
    )
    assert (
        ok_data(await client.get(org_url(org, f"/grades/{snr}"), headers=owner.headers))[
            "staff_count"
        ]
        == 1
    )
    assert first["grade"]["code"] == "SNR"


@pytest.mark.parametrize(
    ("role", "can_write"),
    [
        ("owner", True),
        ("admin", True),
        ("partner", True),
        ("manager", False),
        ("staff", False),
        ("billing", False),
        ("viewer", False),
    ],
)
async def test_permissions_per_role(client: AsyncClient, role: str, can_write: bool) -> None:
    owner, org = await practice(client)
    actor = owner if role == "owner" else await staff(org, role)
    grade = await grade_id(org, "MGR")
    # Every system role can read grades (member.read).
    assert (await client.get(org_url(org, "/grades"), headers=actor.headers)).status_code == 200
    assert (
        await client.get(org_url(org, f"/grades/{grade}"), headers=actor.headers)
    ).status_code == 200
    expected = (201, 200, 200, 200, 200) if can_write else (403,) * 5
    created = await client.post(
        org_url(org, "/grades"), headers=actor.headers, json={"code": "NEW", "name": "New"}
    )
    patched = await client.patch(
        org_url(org, f"/grades/{grade}"), headers=actor.headers, json={"description": "x"}
    )
    off = await client.post(org_url(org, f"/grades/{grade}/deactivate"), headers=actor.headers)
    on = await client.post(org_url(org, f"/grades/{grade}/activate"), headers=actor.headers)
    deleted = await client.delete(org_url(org, f"/grades/{grade}"), headers=actor.headers)
    assert (
        created.status_code,
        patched.status_code,
        off.status_code,
        on.status_code,
        deleted.status_code,
    ) == expected


async def test_the_cost_default_is_hidden_without_rate_read(client: AsyncClient) -> None:
    owner, org = await practice(client)
    created = await create_grade(
        client, owner, org, default_cost_rate=2_500, default_charge_rate=9_500
    )
    manager, plain = await staff(org, "manager"), await staff(org, "staff")
    await add_custom_role(org["id"], "Rate editor", ["member.read", "rate.manage"])
    editor = await add_member(org["id"], "Rate editor")
    for actor, sees_cost in ((owner, True), (manager, True), (plain, False), (editor, False)):
        grade = ok_data(
            await client.get(org_url(org, f"/grades/{created['id']}"), headers=actor.headers)
        )
        assert (grade["default_cost_rate"] == 2_500) is sees_cost, actor.email
        assert grade["default_charge_rate"] == 9_500
        listed = ok_data(await client.get(org_url(org, "/grades"), headers=actor.headers))
        assert any(g["default_cost_rate"] is not None for g in listed) is sees_cost
    # Someone who can change rates but not read costs gets nothing back about them.
    response = await client.post(
        org_url(org, "/grades"),
        headers=editor.headers,
        json={"code": "E1", "name": "E1", "default_cost_rate": 777},
    )
    assert response.status_code == 201 and response.json()["data"]["default_cost_rate"] is None
    assert "777" not in response.text
    stored = await sql(
        "SELECT default_cost_rate FROM grades WHERE code = 'E1' AND organization_id = :o",
        o=org["id"],
    )
    assert stored[0]["default_cost_rate"] == 777


async def test_other_practices_grades_are_invisible(client: AsyncClient) -> None:
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other & Co")
    theirs = await grade_id(other, "SNR")
    url = org_url(org, f"/grades/{theirs}")
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    assert (
        await client.patch(url, headers=owner.headers, json={"name": "Hacked"})
    ).status_code == 404
    assert (await client.post(f"{url}/deactivate", headers=owner.headers)).status_code == 404
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    response = await client.post(
        org_url(org, "/staff"),
        headers=owner.headers,
        json={"full_name": "X", "grade_id": theirs, "start_date": "2026-01-05"},
    )
    assert response.status_code == 422
    # Code and name uniqueness is per practice.
    await create_grade(client, other_owner, other, code="TEAM", name="Team")
    await create_grade(client, owner, org, code="TEAM", name="Team")
    assert (await client.get(org_url(other, "/grades"), headers=owner.headers)).status_code == 404

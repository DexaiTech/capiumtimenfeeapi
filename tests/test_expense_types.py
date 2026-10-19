"""Expense types: the defaults of a practice, CRUD, uniqueness, the used-type rule, permissions,
tenancy, audit rows and the list."""

import pytest

from app.core.permissions import SYSTEM_ROLES
from tests.billing_helpers import count_rows
from tests.clients_helpers import sql
from tests.domain_factories import vat_rate_id
from tests.expense_helpers import expense_team, make_claim, make_line, type_id
from tests.factories import audit_entries, error
from tests.time_helpers import err, ok_data

TYPE_KEYS = {
    "id",
    "name",
    "code",
    "default_vat_rate_id",
    "is_mileage",
    "default_billable",
    "is_active",
    "is_used",
    "can_delete",
    "created_at",
    "updated_at",
}
# code: (name, kind of the default VAT rate, is mileage, billable by default)
DEFAULTS = {
    "TRAVEL": ("Travel", "zero", False, True),
    "MILEAGE": ("Mileage", "outside_scope", True, True),
    "SUBSISTENCE": ("Subsistence", "standard", False, False),
    "ACCOM": ("Accommodation", "standard", False, True),
    "POSTAGE": ("Postage", "exempt", False, True),
    "PRINTING": ("Printing", "standard", False, True),
    "SOFTWARE": ("Software", "standard", False, False),
    "OTHER": ("Other", "standard", False, False),
}


async def listed(client, team, actor=None, **params) -> list[dict]:
    response = await client.get(
        team.url("/expense-types"), headers=(actor or team.owner).headers, params=params
    )
    return ok_data(response)


async def create(client, team, name="Parking", code="PARK", actor=None, status=201, **extra):
    response = await client.post(
        team.url("/expense-types"),
        headers=(actor or team.owner).headers,
        json={"name": name, "code": code, **extra},
    )
    return ok_data(response, 201) if status == 201 else err(response, status)


# --- The defaults ---------------------------------------------------------------------------


async def test_a_new_practice_starts_with_the_eight_default_types(client):
    team = await expense_team(client)
    data = await listed(client, team, limit=100)
    assert [t["code"] for t in data] == sorted(DEFAULTS, key=lambda code: DEFAULTS[code][0].lower())
    for row in data:
        name, kind, mileage, billable = DEFAULTS[row["code"]]
        assert set(row) == TYPE_KEYS
        assert (row["name"], row["is_mileage"], row["default_billable"]) == (
            name,
            mileage,
            billable,
        )
        assert row["is_active"] is True and row["is_used"] is False and row["can_delete"] is True
        rate = await sql(
            "SELECT code, organization_id FROM vat_rates WHERE id = :i",
            i=row["default_vat_rate_id"],
        )
        assert (rate[0]["code"], str(rate[0]["organization_id"])) == (kind, team.id)


async def test_every_practice_has_its_own_types(client):
    first = await expense_team(client)
    second = await expense_team(client, "Second Practice")
    mine = {t["id"] for t in await listed(client, first, limit=100)}
    theirs = {t["id"] for t in await listed(client, second, limit=100)}
    assert len(mine) == len(theirs) == 8 and mine.isdisjoint(theirs)


# --- CRUD -----------------------------------------------------------------------------------


async def test_create_get_update_and_delete(client):
    team = await expense_team(client)
    h = team.owner.headers
    reduced = await vat_rate_id(team.org, "reduced")
    data = await create(
        client, team, "Parking", "park", default_vat_rate_id=str(reduced), default_billable=True
    )
    assert set(data) == TYPE_KEYS
    assert data["code"] == "PARK" and data["default_vat_rate_id"] == str(reduced)
    assert data["default_billable"] is True and data["is_mileage"] is False
    assert data["is_active"] is True and data["is_used"] is False and data["can_delete"] is True

    got = await client.get(team.url(f"/expense-types/{data['id']}"), headers=h)
    assert ok_data(got) == data

    patched = await client.patch(
        team.url(f"/expense-types/{data['id']}"),
        headers=h,
        json={"name": "Car parking", "default_vat_rate_id": None, "is_active": False},
    )
    changed = ok_data(patched)
    assert changed["name"] == "Car parking" and changed["default_vat_rate_id"] is None
    assert changed["is_active"] is False

    deleted = await client.delete(team.url(f"/expense-types/{data['id']}"), headers=h)
    assert deleted.status_code == 200 and deleted.json()["data"] is None
    assert (
        await client.get(team.url(f"/expense-types/{data['id']}"), headers=h)
    ).status_code == 404

    actions = [
        e["action"] for e in await audit_entries(team.id) if e["target_type"] == "expense_type"
    ]
    assert actions == ["expense_type.created", "expense_type.updated", "expense_type.deleted"]
    meta = (await audit_entries(team.id, "expense_type.updated"))[0]["metadata"]
    assert meta["changes"]["name"] == ["Parking", "Car parking"]
    assert meta["changes"]["is_active"] == [True, False]


async def test_validation(client):
    team = await expense_team(client)
    for body in (
        {"name": "", "code": "X1"},
        {"name": "   ", "code": "X1"},
        {"name": "N" * 81, "code": "X1"},
        {"name": "Ok", "code": ""},
        {"name": "Ok", "code": "TOOLONGCODE123"},
        {"name": "Ok", "code": "BAD CODE"},
        {"name": "Ok", "code": "A/B"},
        {"name": "Ok"},
        {"code": "X1"},
        {"name": "Ok", "code": "X1", "unknown": 1},
        {"name": "Ok", "code": "X1", "is_mileage": "maybe"},
    ):
        response = await client.post(
            team.url("/expense-types"), headers=team.owner.headers, json=body
        )
        assert response.status_code == 422, body
    assert await count_rows("SELECT count(*) FROM expense_types") == 8

    made = await create(client, team)
    url = team.url(f"/expense-types/{made['id']}")
    for body in (
        {"name": None},
        {"code": None},
        {"is_mileage": None},
        {"is_active": None},
        {"x": 1},
    ):
        response = await client.patch(url, headers=team.owner.headers, json=body)
        assert response.status_code == 422, body


async def test_the_default_vat_rate_must_be_a_live_rate_of_the_practice(client):
    team = await expense_team(client)
    other = await expense_team(client, "Other Practice")
    foreign = await vat_rate_id(other.org)
    archived = await vat_rate_id(team.org, "reduced")
    await sql("UPDATE vat_rates SET archived_at = now() WHERE id = :i", i=archived)
    for rate in (foreign, archived, "00000000-0000-4000-8000-000000000000"):
        response = await client.post(
            team.url("/expense-types"),
            headers=team.owner.headers,
            json={"name": "Odd", "code": "ODD", "default_vat_rate_id": str(rate)},
        )
        assert response.status_code == 422, rate
        assert error(response)["details"][0]["field"] == "default_vat_rate_id"
    made = await create(client, team)
    response = await client.patch(
        team.url(f"/expense-types/{made['id']}"),
        headers=team.owner.headers,
        json={"default_vat_rate_id": str(foreign)},
    )
    assert response.status_code == 422


# --- Uniqueness -----------------------------------------------------------------------------


async def test_the_name_is_unique_among_active_types_ignoring_case(client):
    team = await expense_team(client)
    first = await create(client, team, "Parking", "PARK")
    taken = await create(client, team, "  parking  ", "PARK2", status=409)
    assert taken["code"] == "EXPENSE_TYPE_NAME_TAKEN"
    assert taken["details"][0]["field"] == "name"
    # A deactivated type gives its name up ...
    await client.patch(
        team.url(f"/expense-types/{first['id']}"),
        headers=team.owner.headers,
        json={"is_active": False},
    )
    second = await create(client, team, "PARKING", "PARK2")
    # ... and cannot take it back while the new type holds it.
    back = await client.patch(
        team.url(f"/expense-types/{first['id']}"),
        headers=team.owner.headers,
        json={"is_active": True},
    )
    assert err(back, 409)["code"] == "EXPENSE_TYPE_NAME_TAKEN"
    # Renaming into a taken name fails too; renaming to itself does not.
    clash = await client.patch(
        team.url(f"/expense-types/{second['id']}"),
        headers=team.owner.headers,
        json={"name": "Travel"},
    )
    assert err(clash, 409)["code"] == "EXPENSE_TYPE_NAME_TAKEN"
    same = await client.patch(
        team.url(f"/expense-types/{second['id']}"),
        headers=team.owner.headers,
        json={"name": "parking", "code": "PARK2"},
    )
    assert same.status_code == 200
    # An inactive type may be created with a name that is taken.
    inactive = await create(client, team, "Travel", "TRAVEL2", is_active=False)
    assert inactive["is_active"] is False


async def test_the_code_is_unique_among_all_types(client):
    team = await expense_team(client)
    clash = await create(client, team, "Rail", "travel", status=409)
    assert clash["code"] == "EXPENSE_TYPE_CODE_TAKEN" and clash["details"][0]["field"] == "code"
    other = await create(client, team, "Rail", "RAIL")
    await client.patch(
        team.url(f"/expense-types/{other['id']}"),
        headers=team.owner.headers,
        json={"is_active": False},
    )
    # The code of an inactive type stays taken.
    assert (await create(client, team, "Rail 2", "RAIL", status=409))["code"] == (
        "EXPENSE_TYPE_CODE_TAKEN"
    )
    clash = await client.patch(
        team.url(f"/expense-types/{other['id']}"),
        headers=team.owner.headers,
        json={"code": "OTHER"},
    )
    assert err(clash, 409)["code"] == "EXPENSE_TYPE_CODE_TAKEN"
    # Another practice may use the same name and code.
    second = await expense_team(client, "Second Practice")
    assert (await create(client, second, "Rail", "RAIL"))["code"] == "RAIL"


# --- A used type ----------------------------------------------------------------------------


async def test_a_used_type_cannot_be_deleted_only_deactivated(client):
    team = await expense_team(client)
    h = team.owner.headers
    travel = await type_id(team.org, "TRAVEL")
    claim = await make_claim(team.org, team.owner)
    await make_line(team.org, claim, code="TRAVEL")

    row = next(t for t in await listed(client, team, limit=100) if t["id"] == str(travel))
    assert row["is_used"] is True and row["can_delete"] is False

    refused = await client.delete(team.url(f"/expense-types/{travel}"), headers=h)
    assert err(refused, 409)["code"] == "EXPENSE_TYPE_IN_USE"
    assert await count_rows("SELECT count(*) FROM expense_types WHERE id = :i", i=str(travel)) == 1

    # Deactivating is the way out, and the history keeps pointing at it.
    off = await client.patch(
        team.url(f"/expense-types/{travel}"), headers=h, json={"is_active": False}
    )
    assert ok_data(off)["is_active"] is False

    # An unused default can still be deleted.
    postage = await type_id(team.org, "POSTAGE")
    assert (
        await client.delete(team.url(f"/expense-types/{postage}"), headers=h)
    ).status_code == 200


async def test_a_used_type_cannot_become_or_stop_being_a_mileage_type(client):
    team = await expense_team(client)
    h = team.owner.headers
    travel = await type_id(team.org, "TRAVEL")
    printing = await type_id(team.org, "PRINTING")
    claim = await make_claim(team.org, team.owner)
    await make_line(team.org, claim, code="TRAVEL")

    refused = await client.patch(
        team.url(f"/expense-types/{travel}"), headers=h, json={"is_mileage": True}
    )
    assert err(refused, 409)["code"] == "EXPENSE_TYPE_IN_USE"
    # Unchanged, or other fields, are fine; an unused type may change.
    same = await client.patch(
        team.url(f"/expense-types/{travel}"),
        headers=h,
        json={"is_mileage": False, "name": "Trains"},
    )
    assert same.status_code == 200
    free = await client.patch(
        team.url(f"/expense-types/{printing}"), headers=h, json={"is_mileage": True}
    )
    assert ok_data(free)["is_mileage"] is True


# --- Permissions and tenancy ----------------------------------------------------------------


@pytest.mark.parametrize("role", [r.key for r in SYSTEM_ROLES])
async def test_types_are_readable_with_log_read_all_or_manage_and_writable_with_manage(
    client, role
):
    team = await expense_team(client)
    actor = await team.member(role)
    grants = next(r.permissions for r in SYSTEM_ROLES if r.key == role)
    can_read = bool(grants & {"expense.log", "expense.read_all", "expense.manage"})
    can_write = "expense.manage" in grants
    some = await type_id(team.org, "OTHER")

    reads = [
        await client.get(team.url("/expense-types"), headers=actor.headers),
        await client.get(team.url(f"/expense-types/{some}"), headers=actor.headers),
    ]
    assert [r.status_code for r in reads] == ([200, 200] if can_read else [403, 403])
    created = await client.post(
        team.url("/expense-types"), headers=actor.headers, json={"name": "New", "code": "NEW"}
    )
    patched = await client.patch(
        team.url(f"/expense-types/{some}"), headers=actor.headers, json={"name": "Other things"}
    )
    deleted = await client.delete(team.url(f"/expense-types/{some}"), headers=actor.headers)
    assert [created.status_code, patched.status_code, deleted.status_code] == (
        [201, 200, 200] if can_write else [403, 403, 403]
    )


async def test_another_practices_types_are_not_found(client):
    team = await expense_team(client)
    other = await expense_team(client, "Other Practice")
    theirs = await type_id(other.org, "TRAVEL")
    h = team.owner.headers
    for response in (
        await client.get(team.url(f"/expense-types/{theirs}"), headers=h),
        await client.patch(team.url(f"/expense-types/{theirs}"), headers=h, json={"name": "X"}),
        await client.delete(team.url(f"/expense-types/{theirs}"), headers=h),
    ):
        assert err(response, 404)["code"] == "RESOURCE_NOT_FOUND"
    # The other practice's URL is closed to a member of this one.
    assert (
        await client.get(other.url("/expense-types"), headers=team.owner.headers)
    ).status_code == 404
    assert await count_rows("SELECT count(*) FROM expense_types WHERE id = :i", i=str(theirs)) == 1
    assert (await client.get(team.url("/expense-types/not-a-uuid"), headers=h)).status_code == 422


# --- The list -------------------------------------------------------------------------------


async def test_list_filters_sorts_and_pages(client):
    team = await expense_team(client)
    h = team.owner.headers
    assert [t["code"] for t in await listed(client, team, q="mile")] == ["MILEAGE"]
    assert [t["code"] for t in await listed(client, team, q="ACCOM")] == ["ACCOM"]
    assert {t["code"] for t in await listed(client, team, is_mileage="true")} == {"MILEAGE"}
    assert len(await listed(client, team, is_mileage="false", limit=100)) == 7
    await client.patch(
        team.url(f"/expense-types/{await type_id(team.org, 'OTHER')}"),
        headers=h,
        json={"is_active": False},
    )
    assert [t["code"] for t in await listed(client, team, is_active="false")] == ["OTHER"]
    assert len(await listed(client, team, is_active="true", limit=100)) == 7
    # Sorting by code, both ways.
    codes = [t["code"] for t in await listed(client, team, sort="code", limit=100)]
    assert codes == sorted(DEFAULTS)
    reverse = [t["code"] for t in await listed(client, team, sort="code", order="desc", limit=100)]
    assert reverse == sorted(DEFAULTS, reverse=True)

    # Cursor pagination walks the whole list once.
    seen, cursor = [], None
    for _ in range(10):
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        response = await client.get(team.url("/expense-types"), headers=h, params=params)
        seen += [t["code"] for t in ok_data(response)]
        cursor = response.json()["meta"]["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 8
    assert seen == [t["code"] for t in await listed(client, team, limit=100)]

    # A cursor belongs to the sort it came from; a made-up one is refused.
    first = await client.get(team.url("/expense-types"), headers=h, params={"limit": 2})
    cursor = first.json()["meta"]["next_cursor"]
    for params in (
        {"cursor": cursor, "sort": "code"},
        {"cursor": cursor, "order": "desc"},
        {"cursor": "not-a-cursor"},
    ):
        response = await client.get(team.url("/expense-types"), headers=h, params=params)
        assert err(response, 422)["details"][0]["field"] == "cursor"
    for params in ({"sort": "id"}, {"limit": 0}, {"limit": 101}, {"is_active": "maybe"}):
        response = await client.get(team.url("/expense-types"), headers=h, params=params)
        assert response.status_code == 422, params

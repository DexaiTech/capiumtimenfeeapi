"""The practice settings of the expense, WIP, invoice-approval and rate modules: what a new practice
starts with (including the 6-minute time increment), their bounds, the audit of a change, who may
change them and the checks the database keeps whatever the API does."""

import asyncio

import pytest
from sqlalchemy.exc import IntegrityError

from app.maintenance.bootstrap import bootstrap
from tests.clients_helpers import sql
from tests.factories import (
    API,
    add_member,
    audit_entries,
    create_user_with_org,
    error,
)

ORGS = f"{API}/organizations"

DEFAULTS = {
    "time_increment_minutes": 6,
    "fixed_fee_wip_mode": "on_completion",
    "wip_adjustment_approval_threshold": 50_000,
    "receipt_required_threshold": 2_500,
    "invoice_approval_required": False,
    "invoice_approval_threshold": 0,
    "mileage_rate": 45,
    "expense_markup_bp": 0,
    "rate_mode": "staff_first",
}
CHANGED = {
    "time_increment_minutes": 15,
    "fixed_fee_wip_mode": "by_budget_progress",
    "wip_adjustment_approval_threshold": 100_000,
    "receipt_required_threshold": 0,
    "invoice_approval_required": True,
    "invoice_approval_threshold": 250_000,
    "mileage_rate": 50,
    "expense_markup_bp": 1_500,
    "rate_mode": "grade_only",
}
NEW_FIELDS = tuple(name for name in DEFAULTS if name != "time_increment_minutes")


async def patch(client, actor, org: dict, body: dict):
    return await client.patch(f"{ORGS}/{org['id']}", headers=actor.headers, json=body)


async def settings_of(client, actor, org: dict) -> dict:
    response = await client.get(f"{ORGS}/{org['id']}", headers=actor.headers)
    return response.json()["data"]["organization"]


# --- Defaults ----------------------------------------------------------------------------------


async def test_a_new_practice_gets_the_documented_defaults(client):
    owner, org = await create_user_with_org(client)
    assert {name: org[name] for name in DEFAULTS} == DEFAULTS
    # The same in every response that carries the practice.
    assert {name: (await settings_of(client, owner, org))[name] for name in DEFAULTS} == DEFAULTS
    listed = (await client.get(f"{API}/users/me/organizations", headers=owner.headers)).json()
    assert {name: listed["data"][0]["organization"][name] for name in DEFAULTS} == DEFAULTS


async def test_the_bootstrap_command_gives_the_same_defaults(db):
    result = await bootstrap(
        db, org_name="Acme", admin_name="Ada Admin", admin_email="ada@acme.example"
    )
    organization = result.organization
    assert {name: getattr(organization, name) for name in DEFAULTS} == DEFAULTS
    lock = await sql(
        "SELECT locked_through FROM period_locks WHERE organization_id = :o", o=organization.id
    )
    assert lock == [{"locked_through": None}]


async def test_existing_values_are_not_touched_by_the_new_default(client):
    """The 6-minute default is for new practices: a practice that chose 1 minute keeps it."""
    owner, org = await create_user_with_org(client)
    assert (await patch(client, owner, org, {"time_increment_minutes": 1})).status_code == 200
    assert (await settings_of(client, owner, org))["time_increment_minutes"] == 1


# --- Changing them -----------------------------------------------------------------------------


async def test_every_setting_can_be_changed_and_is_audited(client):
    owner, org = await create_user_with_org(client)
    response = await patch(client, owner, org, CHANGED)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert {name: data[name] for name in CHANGED} == CHANGED
    assert {name: (await settings_of(client, owner, org))[name] for name in CHANGED} == CHANGED

    [entry] = await audit_entries(org["id"], "organization.updated")
    assert entry["actor_user_id"] == str(owner.id) and entry["target_id"] == org["id"]
    assert entry["metadata"]["changes"] == {
        name: [DEFAULTS[name], value] for name, value in CHANGED.items()
    }
    # Saying what it already is writes nothing.
    again = await patch(client, owner, org, CHANGED)
    assert again.status_code == 200
    assert len(await audit_entries(org["id"], "organization.updated")) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"wip_adjustment_approval_threshold": 0},
        {"wip_adjustment_approval_threshold": 100_000_000},
        {"receipt_required_threshold": 100_000_000},
        {"invoice_approval_threshold": 100_000_000},
        {"mileage_rate": 0},
        {"mileage_rate": 1_000},
        {"expense_markup_bp": 0},
        {"expense_markup_bp": 100_000},
    ],
)
async def test_the_bounds_themselves_are_allowed(client, body):
    owner, org = await create_user_with_org(client)
    response = await patch(client, owner, org, body)
    assert response.status_code == 200, response.text
    [(name, value)] = body.items()
    assert response.json()["data"][name] == value


@pytest.mark.parametrize(
    "body",
    [
        {"fixed_fee_wip_mode": "never"},
        {"fixed_fee_wip_mode": ""},
        {"fixed_fee_wip_mode": 1},
        {"wip_adjustment_approval_threshold": -1},
        {"wip_adjustment_approval_threshold": 100_000_001},
        {"wip_adjustment_approval_threshold": 12.5},
        {"wip_adjustment_approval_threshold": "lots"},
        {"receipt_required_threshold": -1},
        {"receipt_required_threshold": 100_000_001},
        {"receipt_required_threshold": 25.5},
        {"invoice_approval_required": "maybe"},
        {"invoice_approval_threshold": -1},
        {"invoice_approval_threshold": 100_000_001},
        {"invoice_approval_threshold": 0.5},
        {"mileage_rate": -1},
        {"mileage_rate": 1_001},
        {"mileage_rate": 0.45},
        {"expense_markup_bp": -1},
        {"expense_markup_bp": 100_001},
        {"expense_markup_bp": 12.5},
        {"rate_mode": "staff"},
        {"rate_mode": "grade"},
        {"rate_mode": ""},
    ],
)
async def test_values_outside_the_bounds_are_422_on_their_field(client, body):
    owner, org = await create_user_with_org(client)
    before = await settings_of(client, owner, org)
    response = await patch(client, owner, org, body)
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "VALIDATION_ERROR"
    assert error(response)["details"][0]["field"] == next(iter(body))
    assert await settings_of(client, owner, org) == before
    assert await audit_entries(org["id"], "organization.updated") == []


@pytest.mark.parametrize("field", NEW_FIELDS)
async def test_the_new_settings_cannot_be_null(client, field):
    owner, org = await create_user_with_org(client)
    response = await patch(client, owner, org, {field: None})
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == field


async def test_concurrent_patches_of_different_new_settings_both_apply(client):
    owner, org = await create_user_with_org(client)
    first, second, third = await asyncio.gather(
        patch(client, owner, org, {"mileage_rate": 55}),
        patch(client, owner, org, {"expense_markup_bp": 2_000}),
        patch(client, owner, org, {"rate_mode": "grade_only"}),
    )
    assert first.status_code == second.status_code == third.status_code == 200
    final = await settings_of(client, owner, org)
    assert (final["mileage_rate"], final["expense_markup_bp"], final["rate_mode"]) == (
        55,
        2_000,
        "grade_only",
    )
    assert len(await audit_entries(org["id"], "organization.updated")) == 3


# --- Who may and where ---------------------------------------------------------------------------


async def test_changing_them_needs_organization_update_and_every_member_can_read_them(client):
    _, org = await create_user_with_org(client)
    expected = {
        "admin": 200,
        "partner": 200,
        "manager": 403,
        "staff": 403,
        "billing": 403,
        "viewer": 403,
    }
    for index, (role, status) in enumerate(expected.items()):
        actor = await add_member(org["id"], role)
        response = await patch(client, actor, org, {"mileage_rate": 60 + index})
        assert response.status_code == status, role
        seen = await settings_of(client, actor, org)
        assert seen["receipt_required_threshold"] == 2_500 and seen["rate_mode"] == "staff_first", (
            role
        )
    # A refusal never reaches the audit log.
    assert len(await audit_entries(org["id"], "organization.updated")) == 2


async def test_another_practice_cannot_be_changed(client):
    owner, org = await create_user_with_org(client)
    stranger, other = await create_user_with_org(client, "Other & Co")
    response = await patch(client, stranger, org, CHANGED)
    assert response.status_code == 404 and error(response)["code"] == "RESOURCE_NOT_FOUND"
    assert {name: (await settings_of(client, owner, org))[name] for name in DEFAULTS} == DEFAULTS
    assert await audit_entries(org["id"], "organization.updated") == []
    # And the other practice keeps its own.
    assert (await patch(client, stranger, other, {"mileage_rate": 70})).status_code == 200
    assert (await settings_of(client, owner, org))["mileage_rate"] == 45


# --- The database keeps them in range too --------------------------------------------------------


@pytest.mark.parametrize(
    ("assignment", "constraint"),
    [
        ("rate_mode = 'staff'", "ck_organizations_rate_mode"),
        ("fixed_fee_wip_mode = 'never'", "ck_organizations_fixed_fee_wip_mode"),
        (
            "wip_adjustment_approval_threshold = -1",
            "ck_organizations_wip_adjustment_approval_threshold",
        ),
        ("receipt_required_threshold = -1", "ck_organizations_receipt_required_threshold"),
        ("invoice_approval_threshold = -1", "ck_organizations_invoice_approval_threshold"),
        ("mileage_rate = -1", "ck_organizations_mileage_rate"),
        ("expense_markup_bp = -1", "ck_organizations_expense_markup_bp"),
        ("expense_markup_bp = 100001", "ck_organizations_expense_markup_bp"),
        ("time_increment_minutes = 7", "ck_organizations_increment"),
    ],
)
async def test_the_database_checks(client, assignment, constraint):
    _, org = await create_user_with_org(client)
    with pytest.raises(IntegrityError, match=constraint):
        await sql(f"UPDATE organizations SET {assignment} WHERE id = :o", o=org["id"])  # noqa: S608

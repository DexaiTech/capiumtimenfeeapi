"""Practice billing settings (PATCH /organizations/{id}) and the defaults a new practice gets."""

import asyncio

import pytest
from sqlalchemy import text

from app.services.practice_defaults import ACTIVITY_CODES, GRADES, SERVICES, UK_VAT_RATES
from app.services.sequences import DEFAULT_SEQUENCES
from tests.factories import (
    API,
    add_member,
    audit_entries,
    create_org,
    create_user,
    create_user_with_org,
    error,
)

ORGS = f"{API}/organizations"

BILLING = {
    "vat_registered": False,
    "vat_number": "GB123456789",
    "company_number": "01234567",
    "payment_terms_days": 14,
    "default_charge_rate": 12_500,
    "time_increment_minutes": 15,
    "require_timesheet_approval": False,
    "email": "accounts@acme.example",
    "phone": "+44 20 7946 0000",
    "address_line1": "1 High Street",
    "address_line2": "Suite 2",
    "city": "Leeds",
    "postcode": "LS1 1AA",
    "country": "IE",
    "bank_details": "Sort code 00-00-00, account 12345678",
    "invoice_footer": "Registered in England and Wales.",
}
NULLABLE = (
    "vat_number",
    "company_number",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "postcode",
    "bank_details",
    "invoice_footer",
)


async def patch(client, actor, org: dict, body: dict):
    return await client.patch(f"{ORGS}/{org['id']}", headers=actor.headers, json=body)


# --- the settings ---


async def test_a_new_practice_has_the_documented_billing_defaults(client):
    owner, org = await create_user_with_org(client)
    assert {key: org[key] for key in (*BILLING, "currency")} == {
        "currency": "GBP",
        "vat_registered": True,
        "vat_number": None,
        "company_number": None,
        "payment_terms_days": 30,
        "default_charge_rate": 0,
        "time_increment_minutes": 6,
        "require_timesheet_approval": True,
        "email": None,
        "phone": None,
        "address_line1": None,
        "address_line2": None,
        "city": None,
        "postcode": None,
        "country": "GB",
        "bank_details": None,
        "invoice_footer": None,
    }
    fetched = (await client.get(f"{ORGS}/{org['id']}", headers=owner.headers)).json()["data"]
    assert fetched["organization"] == org


async def test_currency_and_vat_registration_can_be_chosen_at_creation(client):
    owner = await create_user()
    org = await create_org(client, owner, "Euro Practice", currency="EUR", vat_registered=False)
    assert org["currency"] == "EUR" and org["vat_registered"] is False


async def test_patch_updates_every_billing_setting_and_audits_it(client):
    owner, org = await create_user_with_org(client)
    response = await patch(client, owner, org, BILLING)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data == {**org, **BILLING}
    # Persisted, not just echoed.
    fetched = (await client.get(f"{ORGS}/{org['id']}", headers=owner.headers)).json()["data"]
    assert fetched["organization"] == data

    (entry,) = await audit_entries(org["id"], "organization.updated")
    assert entry["actor_user_id"] == str(owner.id) and entry["target_id"] == org["id"]
    changes = entry["metadata"]["changes"]
    assert set(changes) == set(BILLING)
    assert changes["default_charge_rate"] == [0, 12_500]
    assert changes["time_increment_minutes"] == [6, 15]
    assert changes["vat_number"] == [None, "GB123456789"]


async def test_text_fields_are_trimmed(client):
    owner, org = await create_user_with_org(client)
    response = await patch(
        client, owner, org, {"city": "  York  ", "invoice_footer": "  Thank you.  "}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["city"] == "York" and data["invoice_footer"] == "Thank you."


@pytest.mark.parametrize("blank", [None, "", "   "])
async def test_nullable_fields_are_cleared_with_null_or_an_empty_string(client, blank):
    owner, org = await create_user_with_org(client)
    assert (await patch(client, owner, org, BILLING)).status_code == 200
    response = await patch(client, owner, org, dict.fromkeys(NULLABLE, blank))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert all(data[field] is None for field in NULLABLE)
    # The other settings are untouched.
    assert data["payment_terms_days"] == 14 and data["country"] == "IE"
    entries = await audit_entries(org["id"], "organization.updated")
    assert len(entries) == 2
    assert entries[1]["metadata"]["changes"]["bank_details"] == [BILLING["bank_details"], None]


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "timezone",
        "week_start",
        "working_days",
        "minutes_per_day",
        "vat_registered",
        "payment_terms_days",
        "default_charge_rate",
        "time_increment_minutes",
        "require_timesheet_approval",
        "country",
    ],
)
async def test_required_settings_cannot_be_null(client, field):
    owner, org = await create_user_with_org(client)
    response = await patch(client, owner, org, {field: None})
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == field


@pytest.mark.parametrize("increment", [1, 5, 6, 10, 15, 30, 60])
async def test_every_documented_time_increment_is_accepted(client, increment):
    owner, org = await create_user_with_org(client)
    response = await patch(client, owner, org, {"time_increment_minutes": increment})
    assert response.status_code == 200
    assert response.json()["data"]["time_increment_minutes"] == increment


@pytest.mark.parametrize(
    "body",
    [
        {"time_increment_minutes": 0},
        {"time_increment_minutes": 7},
        {"time_increment_minutes": 20},
        {"time_increment_minutes": 120},
        {"payment_terms_days": -1},
        {"payment_terms_days": 366},
        {"default_charge_rate": -1},
        {"default_charge_rate": 125.5},
        {"default_charge_rate": 10**12 + 1},
        {"default_charge_rate": "lots"},
        {"vat_registered": "sometimes"},
        {"require_timesheet_approval": "maybe"},
        {"email": "not-an-email"},
        {"country": "gb"},
        {"country": "GBR"},
        {"vat_number": "x" * 21},
        {"company_number": "x" * 21},
        {"phone": "1" * 41},
        {"postcode": "x" * 21},
        {"address_line1": "x" * 201},
        {"bank_details": "x" * 501},
        {"invoice_footer": "x" * 1001},
        # The currency is fixed when the practice is created: every amount is stored in it.
        {"currency": "EUR"},
        {"currency": "GBP"},
        {"slug": "renamed"},
        {"created_by": "01a11108-f0b8-73b2-8dd0-19f38e068d0c"},
    ],
)
async def test_patch_validation(client, body):
    owner, org = await create_user_with_org(client)
    response = await patch(client, owner, org, body)
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "VALIDATION_ERROR"
    assert error(response)["details"][0]["field"] == next(iter(body))
    assert await audit_entries(org["id"], "organization.updated") == []
    unchanged = (await client.get(f"{ORGS}/{org['id']}", headers=owner.headers)).json()["data"]
    assert unchanged["organization"] == org


async def test_an_empty_patch_changes_nothing(client):
    owner, org = await create_user_with_org(client)
    response = await patch(client, owner, org, {})
    assert response.status_code == 200 and response.json()["data"] == org
    assert await audit_entries(org["id"], "organization.updated") == []


# --- who may change them ---


async def test_billing_settings_need_organization_update(client):
    _, org = await create_user_with_org(client)
    expected = {
        "admin": 200,
        "partner": 200,
        "manager": 403,
        "staff": 403,
        "billing": 403,
        "viewer": 403,
    }
    for key, status in expected.items():
        actor = await add_member(org["id"], key)
        response = await patch(client, actor, org, {"payment_terms_days": 7})
        assert response.status_code == status, key
    # Every member may read the settings (invoices and time entry depend on them).
    for key in expected:
        actor = await add_member(org["id"], key)
        assert (await client.get(f"{ORGS}/{org['id']}", headers=actor.headers)).status_code == 200


async def test_another_practice_cannot_be_changed(client):
    owner, org = await create_user_with_org(client)
    _, other = await create_user_with_org(client, "Other")
    response = await patch(client, owner, other, {"default_charge_rate": 1})
    assert response.status_code == 404 and error(response)["code"] == "RESOURCE_NOT_FOUND"
    # An Owner elsewhere is nobody here, whatever their role there.
    stranger = await add_member(other["id"], "admin")
    assert (await patch(client, stranger, org, {"payment_terms_days": 1})).status_code == 404
    assert await audit_entries(other["id"], "organization.updated") == []


async def test_concurrent_patches_of_different_settings_both_apply(client):
    owner, org = await create_user_with_org(client)
    first, second = await asyncio.gather(
        patch(client, owner, org, {"payment_terms_days": 10}),
        patch(client, owner, org, {"default_charge_rate": 9_000}),
    )
    assert first.status_code == second.status_code == 200
    final = (await client.get(f"{ORGS}/{org['id']}", headers=owner.headers)).json()["data"]
    assert final["organization"]["payment_terms_days"] == 10
    assert final["organization"]["default_charge_rate"] == 9_000
    assert len(await audit_entries(org["id"], "organization.updated")) == 2


# --- the defaults a new practice is seeded with ---


async def test_practice_creation_seeds_vat_rates_codes_services_and_sequences(client, db):
    _, org = await create_user_with_org(client)
    params = {"org": org["id"]}

    rates = (
        await db.execute(
            text(
                "SELECT name, code, rate_bp, is_default FROM vat_rates "
                "WHERE organization_id = :org AND archived_at IS NULL ORDER BY rate_bp DESC, code"
            ),
            params,
        )
    ).all()
    assert sorted(tuple(r) for r in rates) == sorted(UK_VAT_RATES)
    assert [(r.code, r.rate_bp) for r in rates if r.is_default] == [("standard", 2000)]
    assert {r.code: r.rate_bp for r in rates} == {
        "standard": 2000,
        "reduced": 500,
        "zero": 0,
        "exempt": 0,
        "outside_scope": 0,
    }

    codes = (
        await db.execute(
            text(
                "SELECT code, name, is_chargeable, is_active FROM activity_codes "
                "WHERE organization_id = :org"
            ),
            params,
        )
    ).all()
    assert sorted((c.code, c.name, c.is_chargeable) for c in codes) == sorted(ACTIVITY_CODES)
    assert sum(c.is_chargeable for c in codes) == 7 and len(codes) == 11
    assert all(c.is_active for c in codes)

    services = (
        await db.execute(
            text(
                "SELECT s.code, s.name, s.billing_basis, s.default_fee, s.sort_order, v.code AS vat "
                "FROM services s LEFT JOIN vat_rates v ON v.id = s.vat_rate_id "
                "WHERE s.organization_id = :org ORDER BY s.sort_order"
            ),
            params,
        )
    ).all()
    assert [(s.code, s.name, s.billing_basis, s.default_fee) for s in services] == list(SERVICES)
    assert len(services) == 8 and {s.vat for s in services} == {"standard"}

    sequences = (
        await db.execute(
            text(
                "SELECT kind, prefix, padding, next_number FROM document_sequences "
                "WHERE organization_id = :org"
            ),
            params,
        )
    ).all()
    assert {s.kind: (s.prefix, s.padding) for s in sequences} == DEFAULT_SEQUENCES
    # The founder took the first staff number.
    assert {s.kind: s.next_number for s in sequences} == {
        kind: 2 if kind == "staff" else 1 for kind in DEFAULT_SEQUENCES
    }

    grades = (
        await db.execute(
            text(
                "SELECT code, name, default_chargeable_target_bp, default_cost_rate, "
                "default_charge_rate, is_active FROM grades WHERE organization_id = :org "
                "ORDER BY sort_order"
            ),
            params,
        )
    ).all()
    assert [(g.code, g.name, g.default_chargeable_target_bp) for g in grades] == list(GRADES)
    assert all(g.default_cost_rate is None and g.default_charge_rate is None for g in grades)
    assert all(g.is_active for g in grades)


async def test_every_practice_gets_its_own_copy_of_the_defaults(client, db):
    _, first = await create_user_with_org(client, "First")
    _, second = await create_user_with_org(client, "Second")
    for table in ("vat_rates", "activity_codes", "services", "grades", "document_sequences"):
        counts = (
            await db.execute(
                text(
                    f"SELECT organization_id, count(*) FROM {table} "  # noqa: S608
                    "WHERE organization_id IN (:a, :b) GROUP BY organization_id"
                ),
                {"a": first["id"], "b": second["id"]},
            )
        ).all()
        assert len(counts) == 2 and counts[0][1] == counts[1][1], table
    # A service never points at another practice's VAT rate.
    crossed = await db.scalar(
        text(
            "SELECT count(*) FROM services s JOIN vat_rates v ON v.id = s.vat_rate_id "
            "WHERE v.organization_id <> s.organization_id"
        )
    )
    assert crossed == 0


async def test_a_failed_creation_leaves_no_defaults_behind(client, db):
    owner = await create_user()
    await create_org(client, owner, "Taken", slug="taken")
    other = await create_user()
    response = await client.post(
        ORGS, headers=other.headers, json={"name": "Second", "slug": "taken"}
    )
    assert response.status_code == 409
    assert await db.scalar(text("SELECT count(*) FROM organizations")) == 1
    assert await db.scalar(text("SELECT count(DISTINCT organization_id) FROM vat_rates")) == 1

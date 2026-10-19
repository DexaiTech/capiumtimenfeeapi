"""Practice settings: VAT rates, services, activity codes and number sequences."""

import asyncio
from datetime import date

import pytest

from tests.clients_helpers import create_client, org_url, scalar, sql
from tests.domain_factories import (
    activity_code_id,
    make_client,
    make_job,
    make_time_entry,
    practice,
    service_id,
    staff,
    vat_rate_id,
)
from tests.factories import audit_entries, error

# --- VAT rates --------------------------------------------------------------------------


async def test_seeded_vat_rates_are_listed_to_any_member(client):
    _owner, org = await practice(client)
    viewer = await staff(org, "viewer")
    response = await client.get(org_url(org, "/vat-rates"), headers=viewer.headers)
    assert response.status_code == 200, response.text
    rates = response.json()["data"]
    assert [r["rate_bp"] for r in rates][:2] == [2000, 500]
    assert len(rates) == 5
    defaults = [r for r in rates if r["is_default"]]
    assert [d["code"] for d in defaults] == ["standard"]
    assert set(rates[0]) >= {"id", "name", "code", "rate_bp", "is_default", "is_archived"}


async def test_create_vat_rate_and_make_it_default_unsets_old(client):
    owner, org = await practice(client)
    response = await client.post(
        org_url(org, "/vat-rates"),
        headers=owner.headers,
        json={"name": "Flat 17.5%", "code": "standard", "rate_bp": 1750, "is_default": True},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["is_default"] is True
    assert data["rate_bp"] == 1750
    defaults = await sql(
        "SELECT name FROM vat_rates WHERE organization_id = :o AND is_default", o=org["id"]
    )
    assert defaults == [{"name": "Flat 17.5%"}]
    audits = await audit_entries(org["id"], "vat_rate.created")
    assert audits[0]["metadata"]["rate_bp"] == 1750


@pytest.mark.parametrize(
    "body",
    [
        {"name": "Too high", "code": "standard", "rate_bp": 10001},
        {"name": "Negative", "code": "standard", "rate_bp": -1},
        {"name": "Bad code", "code": "luxury", "rate_bp": 100},
        {"name": "", "code": "zero", "rate_bp": 0},
        {"name": "Extra", "code": "zero", "rate_bp": 0, "colour": "red"},
        {"name": "Float", "code": "zero", "rate_bp": 12.5},
    ],
)
async def test_vat_rate_validation(client, body):
    owner, org = await practice(client)
    response = await client.post(org_url(org, "/vat-rates"), headers=owner.headers, json=body)
    assert response.status_code == 422, response.text


async def test_vat_rate_bounds_are_accepted(client):
    owner, org = await practice(client)
    for bp, name in ((0, "Nil"), (10000, "All")):
        response = await client.post(
            org_url(org, "/vat-rates"),
            headers=owner.headers,
            json={"name": name, "code": "zero", "rate_bp": bp},
        )
        assert response.status_code == 201, response.text


async def test_duplicate_vat_rate_name_is_409(client):
    owner, org = await practice(client)
    response = await client.post(
        org_url(org, "/vat-rates"),
        headers=owner.headers,
        json={"name": "standard 20%", "code": "standard", "rate_bp": 2000},
    )
    assert response.status_code == 409
    assert error(response)["code"] == "VAT_RATE_NAME_TAKEN"


@pytest.mark.parametrize("role", ["manager", "staff", "billing", "viewer"])
async def test_vat_rate_writes_need_settings_manage(client, role):
    _owner, org = await practice(client)
    actor = await staff(org, role)
    rate = await vat_rate_id(org, "zero")
    url = org_url(org, "/vat-rates")
    body = {"name": "X", "code": "zero", "rate_bp": 0}
    assert (await client.post(url, headers=actor.headers, json=body)).status_code == 403
    assert (
        await client.patch(f"{url}/{rate}", headers=actor.headers, json={"name": "Y"})
    ).status_code == 403
    assert (await client.delete(f"{url}/{rate}", headers=actor.headers)).status_code == 403


async def test_admin_can_manage_vat_rates(client):
    _owner, org = await practice(client)
    admin = await staff(org, "admin")
    response = await client.post(
        org_url(org, "/vat-rates"),
        headers=admin.headers,
        json={"name": "New", "code": "reduced", "rate_bp": 750},
    )
    assert response.status_code == 201


async def test_patch_vat_rate_default_archive_and_restore(client):
    owner, org = await practice(client)
    reduced = await vat_rate_id(org, "reduced")
    url = org_url(org, f"/vat-rates/{reduced}")
    response = await client.patch(url, headers=owner.headers, json={"is_default": True})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["is_default"] is True
    standard = await vat_rate_id(org, "standard")
    assert not await scalar("SELECT is_default FROM vat_rates WHERE id = :i", i=standard)

    # Archiving the default leaves the practice without one.
    response = await client.patch(url, headers=owner.headers, json={"is_archived": True})
    data = response.json()["data"]
    assert data["is_archived"] is True and data["is_default"] is False
    listed = (await client.get(org_url(org, "/vat-rates"), headers=owner.headers)).json()["data"]
    assert str(reduced) not in {r["id"] for r in listed}
    everything = (
        await client.get(org_url(org, "/vat-rates?include_archived=true"), headers=owner.headers)
    ).json()["data"]
    assert everything[-1]["id"] == str(reduced)

    # An archived rate cannot become the default...
    response = await client.patch(url, headers=owner.headers, json={"is_default": True})
    assert response.status_code == 422
    # ...unless it is restored in the same request.
    response = await client.patch(
        url, headers=owner.headers, json={"is_default": True, "is_archived": False}
    )
    assert response.status_code == 200
    assert response.json()["data"]["is_default"] is True


async def test_patch_vat_rate_rejects_nulls_and_unsets_default(client):
    owner, org = await practice(client)
    standard = await vat_rate_id(org, "standard")
    url = org_url(org, f"/vat-rates/{standard}")
    assert (
        await client.patch(url, headers=owner.headers, json={"rate_bp": None})
    ).status_code == 422
    response = await client.patch(url, headers=owner.headers, json={"is_default": False})
    assert response.json()["data"]["is_default"] is False
    assert (
        await scalar(
            "SELECT count(*) FROM vat_rates WHERE organization_id = :o AND is_default", o=org["id"]
        )
        == 0
    )


async def test_delete_unused_vat_rate_deletes_used_rate_archives(client):
    owner, org = await practice(client)
    # Nothing seeded uses the reduced rate.
    reduced = await vat_rate_id(org, "reduced")
    response = await client.delete(org_url(org, f"/vat-rates/{reduced}"), headers=owner.headers)
    assert response.status_code == 200
    assert response.json()["data"]["outcome"] == "deleted"
    assert await scalar("SELECT count(*) FROM vat_rates WHERE id = :i", i=reduced) == 0

    # The seeded services use the standard (default) rate.
    standard = await vat_rate_id(org, "standard")
    response = await client.delete(org_url(org, f"/vat-rates/{standard}"), headers=owner.headers)
    assert response.json()["data"]["outcome"] == "archived"
    row = (await sql("SELECT archived_at, is_default FROM vat_rates WHERE id = :i", i=standard))[0]
    assert row["archived_at"] is not None and row["is_default"] is False
    assert [a["action"] for a in await audit_entries(org["id"])][-2:] == [
        "vat_rate.deleted",
        "vat_rate.archived",
    ]


async def test_vat_rate_used_by_an_expense_type_is_archived(client):
    """The seeded expense types default to the zero, exempt, outside-scope and standard rates."""
    owner, org = await practice(client)
    for code in ("zero", "exempt", "outside_scope"):
        rate = await vat_rate_id(org, code)
        response = await client.delete(org_url(org, f"/vat-rates/{rate}"), headers=owner.headers)
        assert response.status_code == 200, code
        assert response.json()["data"]["outcome"] == "archived", code
        assert await scalar("SELECT count(*) FROM vat_rates WHERE id = :i", i=rate) == 1


async def test_vat_rate_used_by_client_or_disbursement_is_archived(client):
    owner, org = await practice(client)
    reduced = await vat_rate_id(org, "reduced")
    await make_client(org, default_vat_rate_id=reduced)
    response = await client.delete(org_url(org, f"/vat-rates/{reduced}"), headers=owner.headers)
    assert response.json()["data"]["outcome"] == "archived"


async def test_other_practice_vat_rate_is_404(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other LLP")
    foreign = await vat_rate_id(other, "zero")
    url = org_url(org, f"/vat-rates/{foreign}")
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    assert (await client.patch(url, headers=owner.headers, json={"name": "X"})).status_code == 404
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    # A non-member cannot see the practice at all.
    assert (
        await client.get(org_url(org, "/vat-rates"), headers=other_owner.headers)
    ).status_code == 404


async def test_concurrent_default_changes_leave_exactly_one_default(client):
    owner, org = await practice(client)
    reduced = await vat_rate_id(org, "reduced")
    zero = await vat_rate_id(org, "zero")
    responses = await asyncio.gather(
        *(
            client.patch(
                org_url(org, f"/vat-rates/{rid}"), headers=owner.headers, json={"is_default": True}
            )
            for rid in (reduced, zero)
        ),
        client.post(
            org_url(org, "/vat-rates"),
            headers=owner.headers,
            json={"name": "Racer", "code": "zero", "rate_bp": 0, "is_default": True},
        ),
    )
    assert [r.status_code for r in responses] == [200, 200, 201]
    count = await scalar(
        "SELECT count(*) FROM vat_rates WHERE organization_id = :o AND is_default", o=org["id"]
    )
    assert count == 1


# --- Services ---------------------------------------------------------------------------


async def test_list_services_any_member_and_include_inactive(client):
    owner, org = await practice(client)
    viewer = await staff(org, "viewer")
    response = await client.get(org_url(org, "/services"), headers=viewer.headers)
    assert response.status_code == 200
    assert len(response.json()["data"]) == 8
    accs = await service_id(org, "ACCS")
    await client.post(org_url(org, f"/services/{accs}/deactivate"), headers=owner.headers)
    active = (await client.get(org_url(org, "/services"), headers=viewer.headers)).json()["data"]
    assert len(active) == 7
    everything = (
        await client.get(org_url(org, "/services?include_inactive=true"), headers=viewer.headers)
    ).json()["data"]
    assert len(everything) == 8


async def test_create_service_uppercases_code_and_rejects_duplicates(client):
    owner, org = await practice(client)
    rate = await vat_rate_id(org, "reduced")
    response = await client.post(
        org_url(org, "/services"),
        headers=owner.headers,
        json={
            "code": "mtd-it",
            "name": "MTD for income tax",
            "billing_basis": "fixed",
            "default_fee": 30000,
            "vat_rate_id": str(rate),
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["code"] == "MTD-IT"
    assert data["default_fee"] == 30000
    assert data["is_active"] is True

    response = await client.post(
        org_url(org, "/services"), headers=owner.headers, json={"code": "Mtd-It", "name": "Again"}
    )
    assert response.status_code == 409
    assert error(response)["code"] == "SERVICE_CODE_TAKEN"


@pytest.mark.parametrize(
    "body",
    [
        {"code": "has space", "name": "X"},
        {"code": "WAYTOOLONGCODE", "name": "X"},
        {"code": "OK", "name": "X", "billing_basis": "daily"},
        {"code": "OK", "name": "X", "default_fee": -1},
        {"code": "OK", "name": ""},
    ],
)
async def test_service_validation(client, body):
    owner, org = await practice(client)
    response = await client.post(org_url(org, "/services"), headers=owner.headers, json=body)
    assert response.status_code == 422, response.text


async def test_service_vat_rate_must_be_live_and_ours(client):
    owner, org = await practice(client)
    _other_owner, other = await practice(client, "Other")
    foreign = await vat_rate_id(other, "zero")
    response = await client.post(
        org_url(org, "/services"),
        headers=owner.headers,
        json={"code": "X1", "name": "X", "vat_rate_id": str(foreign)},
    )
    assert response.status_code == 422
    assert error(response)["details"][0]["field"] == "vat_rate_id"
    zero = await vat_rate_id(org, "zero")
    await client.patch(
        org_url(org, f"/vat-rates/{zero}"), headers=owner.headers, json={"is_archived": True}
    )
    response = await client.post(
        org_url(org, "/services"),
        headers=owner.headers,
        json={"code": "X2", "name": "X", "vat_rate_id": str(zero)},
    )
    assert response.status_code == 422


async def test_update_and_reactivate_service(client):
    owner, org = await practice(client)
    vat = await service_id(org, "VAT")
    url = org_url(org, f"/services/{vat}")
    response = await client.patch(
        url, headers=owner.headers, json={"name": "VAT return (quarterly)", "default_fee": 17500}
    )
    assert response.status_code == 200
    assert response.json()["data"]["default_fee"] == 17500
    assert (await client.patch(url, headers=owner.headers, json={"code": "CT"})).status_code == 409
    assert (await client.patch(url, headers=owner.headers, json={"name": None})).status_code == 422
    response = await client.post(f"{url}/deactivate", headers=owner.headers)
    assert response.json()["data"]["is_active"] is False
    response = await client.post(f"{url}/activate", headers=owner.headers)
    assert response.json()["data"]["is_active"] is True
    changes = (await audit_entries(org["id"], "service.updated"))[0]["metadata"]["changes"]
    assert changes["default_fee"] == [15000, 17500]


async def test_delete_service_unused_deletes_used_deactivates(client):
    owner, org = await practice(client)
    secretarial = await service_id(org, "SECRETARIAL")
    response = await client.delete(org_url(org, f"/services/{secretarial}"), headers=owner.headers)
    assert response.json()["data"]["outcome"] == "deleted"
    assert await scalar("SELECT count(*) FROM services WHERE id = :i", i=secretarial) == 0

    accs = await service_id(org, "ACCS")
    acme = await make_client(org)
    await make_job(org, acme, service_id=accs)
    response = await client.delete(org_url(org, f"/services/{accs}"), headers=owner.headers)
    assert response.json()["data"]["outcome"] == "deactivated"
    assert await scalar("SELECT is_active FROM services WHERE id = :i", i=accs) is False


async def test_service_used_by_engagement_is_deactivated(client):
    owner, org = await practice(client)
    payroll = await service_id(org, "PAYROLL")
    created = await create_client(client, owner, org)
    await client.post(
        org_url(org, f"/clients/{created['id']}/services"),
        headers=owner.headers,
        json={"service_id": str(payroll)},
    )
    response = await client.delete(org_url(org, f"/services/{payroll}"), headers=owner.headers)
    assert response.json()["data"]["outcome"] == "deactivated"


@pytest.mark.parametrize("role", ["manager", "staff", "billing"])
async def test_service_writes_need_settings_manage(client, role):
    _owner, org = await practice(client)
    actor = await staff(org, role)
    accs = await service_id(org, "ACCS")
    url = org_url(org, "/services")
    assert (
        await client.post(url, headers=actor.headers, json={"code": "Z", "name": "Z"})
    ).status_code == 403
    assert (
        await client.patch(f"{url}/{accs}", headers=actor.headers, json={"name": "Z"})
    ).status_code == 403
    assert (await client.post(f"{url}/{accs}/deactivate", headers=actor.headers)).status_code == 403
    assert (await client.delete(f"{url}/{accs}", headers=actor.headers)).status_code == 403


async def test_other_practice_service_is_404(client):
    owner, org = await practice(client)
    _, other = await practice(client, "Other")
    foreign = await service_id(other, "ACCS")
    url = org_url(org, f"/services/{foreign}")
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    assert (await client.patch(url, headers=owner.headers, json={"name": "x"})).status_code == 404
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    assert (await client.post(f"{url}/deactivate", headers=owner.headers)).status_code == 404


# --- Activity codes ---------------------------------------------------------------------


async def test_activity_codes_crud(client):
    owner, org = await practice(client)
    member = await staff(org)
    listed = (await client.get(org_url(org, "/activity-codes"), headers=member.headers)).json()
    assert len(listed["data"]) == 11
    assert listed["data"][0]["is_chargeable"] is True
    assert listed["data"][-1]["is_chargeable"] is False

    response = await client.post(
        org_url(org, "/activity-codes"),
        headers=owner.headers,
        json={"code": "rnd", "name": "R&D claims", "is_chargeable": True},
    )
    assert response.status_code == 201, response.text
    created = response.json()["data"]
    assert created["code"] == "RND"
    dup = await client.post(
        org_url(org, "/activity-codes"), headers=owner.headers, json={"code": "RND", "name": "x"}
    )
    assert dup.status_code == 409 and error(dup)["code"] == "ACTIVITY_CODE_TAKEN"

    url = org_url(org, f"/activity-codes/{created['id']}")
    response = await client.patch(url, headers=owner.headers, json={"is_chargeable": False})
    assert response.json()["data"]["is_chargeable"] is False
    assert (await client.get(url, headers=member.headers)).status_code == 200
    response = await client.post(f"{url}/deactivate", headers=owner.headers)
    assert response.json()["data"]["is_active"] is False
    response = await client.delete(url, headers=owner.headers)
    assert response.json()["data"]["outcome"] == "deleted"
    assert (await client.get(url, headers=member.headers)).status_code == 404


async def test_used_activity_code_is_deactivated_not_deleted(client):
    owner, org = await practice(client)
    member = await staff(org)
    acc = await activity_code_id(org, "ACC")
    await make_time_entry(org, member, activity_code_id=acc)
    response = await client.delete(org_url(org, f"/activity-codes/{acc}"), headers=owner.headers)
    assert response.json()["data"]["outcome"] == "deactivated"
    listed = (await client.get(org_url(org, "/activity-codes"), headers=owner.headers)).json()
    assert str(acc) not in {c["id"] for c in listed["data"]}
    every = (
        await client.get(
            org_url(org, "/activity-codes?include_inactive=true"), headers=owner.headers
        )
    ).json()
    assert str(acc) in {c["id"] for c in every["data"]}


async def test_activity_code_permissions_and_tenancy(client):
    owner, org = await practice(client)
    _, other = await practice(client, "Other")
    manager = await staff(org, "manager")
    acc = await activity_code_id(org, "ACC")
    foreign = await activity_code_id(other, "ACC")
    response = await client.post(
        org_url(org, "/activity-codes"), headers=manager.headers, json={"code": "Q", "name": "Q"}
    )
    assert response.status_code == 403
    assert (
        await client.patch(
            org_url(org, f"/activity-codes/{acc}"), headers=manager.headers, json={"name": "Q"}
        )
    ).status_code == 403
    assert (
        await client.get(org_url(org, f"/activity-codes/{foreign}"), headers=owner.headers)
    ).status_code == 404
    assert (
        await client.delete(org_url(org, f"/activity-codes/{foreign}"), headers=owner.headers)
    ).status_code == 404
    assert (
        await client.post(
            org_url(org, "/activity-codes"), headers=owner.headers, json={"code": "", "name": "Q"}
        )
    ).status_code == 422


# --- Sequences --------------------------------------------------------------------------


async def test_list_sequences_any_member(client):
    _owner, org = await practice(client)
    viewer = await staff(org, "viewer")
    response = await client.get(org_url(org, "/sequences"), headers=viewer.headers)
    assert response.status_code == 200
    by_kind = {s["kind"]: s for s in response.json()["data"]}
    assert set(by_kind) == {
        "client",
        "job",
        "invoice",
        "credit_note",
        "staff",
        "ticket",
        "estimate",
    }
    assert by_kind["invoice"]["preview"] == "INV-00001"
    assert by_kind["client"]["preview"] == "C0001"
    assert by_kind["ticket"]["preview"] == "APT1"
    assert by_kind["estimate"]["preview"] == "EST00001"


async def test_change_sequence_needs_organization_update(client):
    _owner, org = await practice(client)
    url = org_url(org, "/sequences/invoice")
    for role in ("manager", "billing", "staff"):
        actor = await staff(org, role)
        response = await client.patch(url, headers=actor.headers, json={"prefix": "X"})
        assert response.status_code == 403, role
    partner = await staff(org, "partner")
    response = await client.patch(
        url, headers=partner.headers, json={"prefix": "ACME/", "padding": 6, "next_number": 120}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["preview"] == "ACME/000120"
    audit = (await audit_entries(org["id"], "sequence.updated"))[0]["metadata"]
    assert audit["kind"] == "invoice"
    assert audit["changes"]["next_number"] == [1, 120]


@pytest.mark.parametrize("kind", ["invoice", "credit_note", "ticket", "estimate"])
async def test_invoice_credit_note_and_ticket_numbers_only_move_forward(client, kind):
    owner, org = await practice(client)
    url = org_url(org, f"/sequences/{kind}")
    assert (
        await client.patch(url, headers=owner.headers, json={"next_number": 50})
    ).status_code == 200
    response = await client.patch(url, headers=owner.headers, json={"next_number": 49})
    assert response.status_code == 409
    assert error(response)["code"] == "SEQUENCE_BACKWARDS"
    assert (
        await client.patch(url, headers=owner.headers, json={"next_number": 50})
    ).status_code == 200


async def test_job_sequence_may_move_backwards(client):
    owner, org = await practice(client)
    url = org_url(org, "/sequences/job")
    await client.patch(url, headers=owner.headers, json={"next_number": 10})
    response = await client.patch(url, headers=owner.headers, json={"next_number": 3})
    assert response.status_code == 200
    assert response.json()["data"]["next_number"] == 3


async def test_sequence_next_number_already_used_is_409(client):
    owner, org = await practice(client)
    acme = await make_client(org)
    await sql(
        "INSERT INTO invoices (id, organization_id, client_id, number, status, issue_date, "
        "due_date, currency) VALUES (gen_random_uuid(), :o, :c, 'INV-00007', 'issued', "
        ":d, :d, 'GBP')",
        o=org["id"],
        c=str(acme.id),
        d=date(2026, 9, 1),
    )
    response = await client.patch(
        org_url(org, "/sequences/invoice"), headers=owner.headers, json={"next_number": 7}
    )
    assert response.status_code == 409
    assert error(response)["code"] == "SEQUENCE_NUMBER_TAKEN"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"prefix": "A/B"}, 422),  # '/' is not allowed in client codes
        ({"prefix": "TOOLONGPREFX", "padding": 10}, 422),  # longer than 20
        ({"padding": 0}, 422),
        ({"next_number": 0}, 422),
        ({"prefix": "bad prefix"}, 422),
        ({"prefix": None}, 422),
    ],
)
async def test_sequence_validation(client, body, status):
    owner, org = await practice(client)
    response = await client.patch(
        org_url(org, "/sequences/client"), headers=owner.headers, json=body
    )
    assert response.status_code == status, response.text


async def test_unknown_sequence_kind_is_422(client):
    owner, org = await practice(client)
    response = await client.patch(
        org_url(org, "/sequences/quote"), headers=owner.headers, json={"prefix": "Q"}
    )
    assert response.status_code == 422


async def test_client_sequence_drives_generated_codes(client):
    owner, org = await practice(client)
    response = await client.patch(
        org_url(org, "/sequences/client"),
        headers=owner.headers,
        json={"prefix": "acme-", "padding": 3, "next_number": 7},
    )
    assert response.status_code == 200
    created = await create_client(client, owner, org)
    assert created["code"] == "ACME-007"
    seq = (await client.get(org_url(org, "/sequences"), headers=owner.headers)).json()["data"]
    assert {s["kind"]: s["next_number"] for s in seq}["client"] == 8


async def test_client_codes_up_to_the_20_character_limit_then_sequence_invalid(client):
    """The last 20-character code is still handed out (the look-ahead past it used to
    reject the whole request); the first code over 20 characters is 409 SEQUENCE_INVALID."""
    owner, org = await practice(client)
    response = await client.patch(
        org_url(org, "/sequences/client"),
        headers=owner.headers,
        json={"prefix": "ABCDEFGHIJKL", "padding": 8, "next_number": 99_999_999},
    )
    assert response.status_code == 200, response.text
    created = await create_client(client, owner, org, "Last One")
    assert created["code"] == "ABCDEFGHIJKL99999999"
    response = await client.post(
        org_url(org, "/clients"), headers=owner.headers, json={"name": "One Too Many"}
    )
    assert response.status_code == 409
    assert error(response)["code"] == "SEQUENCE_INVALID"
    # An explicit code still works, and nothing was consumed by the failed attempt.
    await create_client(client, owner, org, "Manual", code="MANUAL1")
    seq = (await client.get(org_url(org, "/sequences"), headers=owner.headers)).json()["data"]
    assert {s["kind"]: s["next_number"] for s in seq}["client"] == 100_000_000


async def test_sequence_row_missing_is_created_on_patch(client):
    owner, org = await practice(client)
    await sql("DELETE FROM document_sequences WHERE organization_id = :o", o=org["id"])
    listed = (await client.get(org_url(org, "/sequences"), headers=owner.headers)).json()["data"]
    assert len(listed) == 7 and all(s["next_number"] == 1 for s in listed)
    response = await client.patch(
        org_url(org, "/sequences/credit_note"), headers=owner.headers, json={"next_number": 5}
    )
    assert response.status_code == 200
    assert response.json()["data"]["preview"] == "CN-00005"

"""Client create, read, update, archive, delete, status rules and the client overview."""

import asyncio
import uuid
from datetime import date, timedelta

import pytest

from app.core.exceptions import ConflictError, NotFoundError
from app.db.session import get_sessionmaker
from app.services.clients import ensure_client_accepts_work
from tests.clients_helpers import create_client, org_url, scalar, sql
from tests.domain_factories import (
    make_client,
    make_disbursement,
    make_job,
    make_time_entry,
    practice,
    service_id,
    staff,
    vat_rate_id,
)
from tests.factories import audit_entries, error

# --- Create -----------------------------------------------------------------------------


async def test_create_generates_codes_from_the_client_sequence(client):
    owner, org = await practice(client)
    first = await create_client(client, owner, org, "Acme Widgets Ltd")
    second = await create_client(client, owner, org, "zed")
    assert first["code"] == "C0001" and second["code"] == "C0002"
    assert first["status"] == "active"
    assert first["client_type"] == "limited_company"
    assert first["aml_status"] == "not_started"
    assert first["initials"] == "AW" and second["initials"] == "ZE"
    assert first["contacts"] == [] and first["engagements"] == []
    assert first["deadlines"]["status"] == "none"
    audit = (await audit_entries(org["id"], "client.created"))[0]
    assert audit["metadata"]["code"] == "C0001"
    assert audit["metadata"]["name"] == "Acme Widgets Ltd"


async def test_generated_codes_skip_codes_already_taken(client):
    owner, org = await practice(client)
    await create_client(client, owner, org, "Manual", code="c0001")
    generated = await create_client(client, owner, org, "Generated")
    assert generated["code"] == "C0002"


async def test_create_full_client_with_contacts(client):
    owner, org = await practice(client)
    manager = await staff(org, "manager")
    partner = await staff(org, "partner")
    reduced = await vat_rate_id(org, "reduced")
    response = await client.post(
        org_url(org, "/clients"),
        headers=owner.headers,
        json={
            "code": "acme-01",
            "name": "Acme Widgets Ltd",
            "client_type": "llp",
            "status": "prospect",
            "email": "accounts@acme.example",
            "company_number": "sc 123456",
            "utr": "12345 67890",
            "year_end_month": 2,
            "year_end_day": 29,
            "manager_user_id": str(manager.id),
            "partner_user_id": str(partner.id),
            "payment_terms_days": 14,
            "default_vat_rate_id": str(reduced),
            "segment": "Gold",
            "aml_status": "clear",
            "aml_checked_on": "2026-01-15",
            "health_score": 82,
            "accounting_software": "xero",
            "contacts": [
                {"name": "Jane Finance", "email": "jane@acme.example", "is_billing": True},
                {"name": "Bob Boss", "position": "Director"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["code"] == "ACME-01"
    assert data["company_number"] == "SC123456"
    assert data["utr"] == "1234567890"
    assert data["status"] == "prospect"
    assert data["accounting_software"] == "xero"
    assert data["health_score"] == 82
    contacts = {c["name"]: c for c in data["contacts"]}
    assert contacts["Jane Finance"]["is_primary"] is True
    assert contacts["Bob Boss"]["is_primary"] is False
    assert data["contact_name"] == "Jane Finance"
    assert data["contact_email"] == "jane@acme.example"


async def test_explicit_primary_contact_on_create(client):
    owner, org = await practice(client)
    data = await create_client(
        client,
        owner,
        org,
        contacts=[{"name": "First"}, {"name": "Second", "is_primary": True}],
    )
    assert data["contact_name"] == "Second"
    response = await client.post(
        org_url(org, "/clients"),
        headers=owner.headers,
        json={
            "name": "Two primaries",
            "contacts": [{"name": "A", "is_primary": True}, {"name": "B", "is_primary": True}],
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "x" * 201},
        {"name": "Bad code", "code": "has space"},
        {"name": "Bad code", "code": "X" * 21},
        {"name": "Bad type", "client_type": "plc"},
        {"name": "Bad status", "status": "dormant"},
        {"name": "Bad email", "email": "not-an-email"},
        {"name": "Half year end", "year_end_month": 3},
        {"name": "Feb 30", "year_end_month": 2, "year_end_day": 30},
        {"name": "Health", "health_score": 101},
        {"name": "Terms", "payment_terms_days": 400},
        {"name": "Software", "accounting_software": "excel"},
        {"name": "AML", "aml_status": "ok"},
        {"name": "Country", "country": "gbr"},
        {"name": "Extra", "colour": "blue"},
    ],
)
async def test_create_validation(client, body):
    owner, org = await practice(client)
    response = await client.post(org_url(org, "/clients"), headers=owner.headers, json=body)
    assert response.status_code == 422, response.text


async def test_duplicate_code_is_409(client):
    owner, org = await practice(client)
    await create_client(client, owner, org, code="ACME")
    response = await client.post(
        org_url(org, "/clients"), headers=owner.headers, json={"name": "Copy", "code": "acme"}
    )
    assert response.status_code == 409
    assert error(response)["code"] == "CLIENT_CODE_TAKEN"
    # Another practice may use the same code.
    other_owner, other = await practice(client, "Other")
    await create_client(client, other_owner, other, code="ACME")


async def test_partner_manager_and_vat_rate_must_belong_to_the_practice(client):
    owner, org = await practice(client)
    _, other = await practice(client, "Other")
    outsider = await staff(other, "manager")
    suspended = await staff(org, "manager", status="suspended")
    foreign_rate = await vat_rate_id(other)
    for body, field in (
        ({"manager_user_id": str(outsider.id)}, "manager_user_id"),
        ({"partner_user_id": str(suspended.id)}, "partner_user_id"),
        ({"default_vat_rate_id": str(foreign_rate)}, "default_vat_rate_id"),
    ):
        response = await client.post(
            org_url(org, "/clients"), headers=owner.headers, json={"name": "X", **body}
        )
        assert response.status_code == 422, body
        assert error(response)["details"][0]["field"] == field


async def test_create_permissions(client):
    _owner, org = await practice(client)
    for role in ("staff", "billing", "viewer"):
        actor = await staff(org, role)
        response = await client.post(
            org_url(org, "/clients"), headers=actor.headers, json={"name": "Nope"}
        )
        assert response.status_code == 403, role
    manager = await staff(org, "manager")
    await create_client(client, manager, org, "Managed")
    # Creating an archived client is archiving: it needs client.delete.
    response = await client.post(
        org_url(org, "/clients"),
        headers=manager.headers,
        json={"name": "Old", "status": "archived"},
    )
    assert response.status_code == 403


async def test_concurrent_creates_get_distinct_codes(client):
    owner, org = await practice(client)
    responses = await asyncio.gather(
        *(
            client.post(org_url(org, "/clients"), headers=owner.headers, json={"name": f"C{i}"})
            for i in range(6)
        )
    )
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    codes = sorted(r.json()["data"]["code"] for r in responses)
    assert codes == [f"C000{i}" for i in range(1, 7)]
    seq = await scalar(
        "SELECT next_number FROM document_sequences WHERE organization_id = :o AND kind = 'client'",
        o=org["id"],
    )
    assert seq == 7


# --- Read, update -----------------------------------------------------------------------


async def test_any_client_reader_sees_a_client_other_practices_do_not(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other")
    acme = await create_client(client, owner, org)
    viewer = await staff(org, "viewer")
    response = await client.get(org_url(org, f"/clients/{acme['id']}"), headers=viewer.headers)
    assert response.status_code == 200
    assert response.json()["data"]["name"] == "Acme Widgets Ltd"
    # The other practice's owner cannot reach it through either practice.
    assert (
        await client.get(org_url(org, f"/clients/{acme['id']}"), headers=other_owner.headers)
    ).status_code == 404
    assert (
        await client.get(org_url(other, f"/clients/{acme['id']}"), headers=other_owner.headers)
    ).status_code == 404
    assert (
        await client.get(org_url(org, f"/clients/{uuid.uuid4()}"), headers=owner.headers)
    ).status_code == 404


async def test_update_client(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, year_end_month=3, year_end_day=31)
    url = org_url(org, f"/clients/{acme['id']}")
    response = await client.patch(
        url,
        headers=owner.headers,
        json={"name": "Acme Holdings Ltd", "segment": "Platinum", "email": "", "health_score": 55},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["name"] == "Acme Holdings Ltd"
    assert data["initials"] == "AH"
    assert data["segment"] == "Platinum"
    assert data["email"] is None
    changes = (await audit_entries(org["id"], "client.updated"))[0]["metadata"]["changes"]
    assert changes["name"] == ["Acme Widgets Ltd", "Acme Holdings Ltd"]

    # The year end is checked against the stored half.
    response = await client.patch(
        url, headers=owner.headers, json={"year_end_day": 31, "year_end_month": 4}
    )
    assert response.status_code == 422
    response = await client.patch(
        url, headers=owner.headers, json={"year_end_month": 4, "year_end_day": 30}
    )
    assert response.status_code == 200
    response = await client.patch(url, headers=owner.headers, json={"year_end_month": None})
    assert response.status_code == 422
    response = await client.patch(
        url, headers=owner.headers, json={"year_end_month": None, "year_end_day": None}
    )
    assert response.status_code == 200
    for body in ({"name": None}, {"status": None}, {"code": None}, {"nope": 1}):
        assert (await client.patch(url, headers=owner.headers, json=body)).status_code == 422


async def test_update_code_uniqueness(client):
    owner, org = await practice(client)
    first = await create_client(client, owner, org, "First")
    await create_client(client, owner, org, "Second", code="TAKEN")
    url = org_url(org, f"/clients/{first['id']}")
    response = await client.patch(url, headers=owner.headers, json={"code": "taken"})
    assert response.status_code == 409 and error(response)["code"] == "CLIENT_CODE_TAKEN"
    response = await client.patch(url, headers=owner.headers, json={"code": first["code"]})
    assert response.status_code == 200


async def test_update_permissions_and_tenancy(client):
    owner, org = await practice(client)
    _, other = await practice(client, "Other")
    acme = await create_client(client, owner, org)
    foreign = await make_client(other)
    url = org_url(org, f"/clients/{acme['id']}")
    for role in ("staff", "viewer"):
        actor = await staff(org, role)
        assert (
            await client.patch(url, headers=actor.headers, json={"name": "X"})
        ).status_code == 403
    billing = await staff(org, "billing")
    assert (
        await client.patch(url, headers=billing.headers, json={"payment_terms_days": 7})
    ).status_code == 200
    assert (
        await client.patch(
            org_url(org, f"/clients/{foreign.id}"), headers=owner.headers, json={"name": "X"}
        )
    ).status_code == 404


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("active", "inactive"),
        ("inactive", "prospect"),
        ("prospect", "active"),
        ("active", "prospect"),
    ],
)
async def test_any_status_transition_with_client_update(client, old, new):
    owner, org = await practice(client)
    manager = await staff(org, "manager")
    acme = await create_client(client, owner, org, status=old)
    response = await client.patch(
        org_url(org, f"/clients/{acme['id']}"), headers=manager.headers, json={"status": new}
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == new


async def test_archiving_needs_client_delete(client):
    owner, org = await practice(client)
    manager = await staff(org, "manager")
    partner = await staff(org, "partner")
    acme = await create_client(client, owner, org)
    url = org_url(org, f"/clients/{acme['id']}")
    assert (
        await client.patch(url, headers=manager.headers, json={"status": "archived"})
    ).status_code == 403
    assert (await client.post(f"{url}/archive", headers=manager.headers)).status_code == 403
    response = await client.post(f"{url}/archive", headers=partner.headers)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "archived"
    assert (await audit_entries(org["id"], "client.archived"))[0]["metadata"]["code"] == acme[
        "code"
    ]
    # Restoring is the reverse operation and needs the same permission.
    assert (
        await client.patch(url, headers=manager.headers, json={"status": "active"})
    ).status_code == 403
    assert (
        await client.patch(url, headers=partner.headers, json={"status": "active"})
    ).status_code == 200


# --- Delete -----------------------------------------------------------------------------


async def test_delete_unused_client_frees_its_code(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, code="ACME")
    url = org_url(org, f"/clients/{acme['id']}")
    manager = await staff(org, "manager")
    assert (await client.delete(url, headers=manager.headers)).status_code == 403
    response = await client.delete(url, headers=owner.headers)
    assert response.status_code == 200
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    assert await scalar("SELECT deleted_at IS NOT NULL FROM clients WHERE id = :i", i=acme["id"])
    await create_client(client, owner, org, "New Acme", code="ACME")
    assert (await audit_entries(org["id"], "client.deleted"))[0]["metadata"]["code"] == "ACME"


@pytest.mark.parametrize("kind", ["job", "time", "disbursement", "invoice", "payment"])
async def test_client_in_use_cannot_be_deleted(client, kind):
    owner, org = await practice(client)
    member = await staff(org)
    acme = await make_client(org)
    if kind == "job":
        await make_job(org, acme)
    elif kind == "time":
        await make_time_entry(org, member, client=acme)
    elif kind == "disbursement":
        await make_disbursement(org, acme)
    elif kind == "invoice":
        await sql(
            "INSERT INTO invoices (id, organization_id, client_id, status, currency) "
            "VALUES (gen_random_uuid(), :o, :c, 'draft', 'GBP')",
            o=org["id"],
            c=str(acme.id),
        )
    else:
        await sql(
            "INSERT INTO payments (id, organization_id, client_id, received_on, amount) "
            "VALUES (gen_random_uuid(), :o, :c, '2026-09-01', 100)",
            o=org["id"],
            c=str(acme.id),
        )
    response = await client.delete(org_url(org, f"/clients/{acme.id}"), headers=owner.headers)
    assert response.status_code == 409
    body = error(response)
    assert body["code"] == "CLIENT_IN_USE"
    assert sum(body["details"].values()) == 1
    assert await scalar("SELECT deleted_at FROM clients WHERE id = :i", i=str(acme.id)) is None


async def test_deleted_job_does_not_block_delete(client):
    owner, org = await practice(client)
    acme = await make_client(org)
    await make_job(org, acme, deleted_at=date(2026, 1, 1))
    response = await client.delete(org_url(org, f"/clients/{acme.id}"), headers=owner.headers)
    assert response.status_code == 200


# --- ensure_client_accepts_work ---------------------------------------------------------


@pytest.mark.parametrize("status", ["prospect", "active"])
async def test_ensure_client_accepts_work_for_live_statuses(client, status):
    _, org = await practice(client)
    acme = await make_client(org, status=status)
    async with get_sessionmaker()() as session:
        found = await ensure_client_accepts_work(session, uuid.UUID(org["id"]), acme.id)
        assert found.id == acme.id


@pytest.mark.parametrize("status", ["inactive", "archived"])
async def test_ensure_client_accepts_work_refuses_inactive(client, status):
    _, org = await practice(client)
    acme = await make_client(org, status=status)
    async with get_sessionmaker()() as session:
        with pytest.raises(ConflictError) as raised:
            await ensure_client_accepts_work(session, uuid.UUID(org["id"]), acme.id)
    assert raised.value.code == "CLIENT_INACTIVE"
    assert raised.value.status_code == 409


async def test_ensure_client_accepts_work_missing_deleted_or_foreign(client):
    _, org = await practice(client)
    _, other = await practice(client, "Other")
    deleted = await make_client(org, deleted_at=date(2026, 1, 1))
    foreign = await make_client(other)
    async with get_sessionmaker()() as session:
        for client_id in (deleted.id, foreign.id, uuid.uuid4()):
            with pytest.raises(NotFoundError):
                await ensure_client_accepts_work(session, uuid.UUID(org["id"]), client_id)


# --- The overview -----------------------------------------------------------------------


async def _money_fixture(client):
    owner, org = await practice(client)
    member = await staff(org)
    acme = await make_client(org, name="Acme Widgets Ltd")
    other = await make_client(org, name="Other Client")
    # WIP: approved, billable, unbilled.
    await make_time_entry(org, member, client=acme, minutes=90, charge_rate=12_000)
    # Not WIP: billed / on an open timesheet while approval is required / non-billable.
    await make_time_entry(
        org, member, client=acme, minutes=30, charge_rate=10_000, billing_status="billed"
    )
    await make_time_entry(
        org,
        member,
        client=acme,
        entry_date=date(2026, 9, 14),
        minutes=60,
        timesheet_status="open",
    )
    await make_time_entry(org, member, client=acme, minutes=60, is_billable=False)
    await make_time_entry(org, member, client=other, minutes=600)
    await make_disbursement(org, acme, 1_300)
    await make_disbursement(org, acme, 500, billing_status="billed")
    await make_disbursement(org, acme, 700, is_billable=False)
    past, future = date.today() - timedelta(days=10), date.today() + timedelta(days=20)
    for status, number, gross, paid, due in (
        ("issued", "INV-1", 12_000, 2_000, past),
        ("issued", "INV-2", 6_000, 0, future),
        ("void", "INV-3", 9_000, 0, past),
        ("draft", None, 4_000, 0, None),
    ):
        await sql(
            "INSERT INTO invoices (id, organization_id, client_id, number, status, issue_date, "
            "due_date, currency, net_total, vat_total, gross_total, paid_total) VALUES "
            "(gen_random_uuid(), :o, :c, :n, :s, :i, :d, 'GBP', :g, 0, :g, :p)",
            o=org["id"],
            c=str(acme.id),
            n=number,
            s=status,
            i=past - timedelta(days=30) if status != "draft" else None,
            d=due,
            g=gross,
            p=paid,
        )
    await sql(
        "INSERT INTO payments (id, organization_id, client_id, received_on, amount, "
        "allocated_total) VALUES (gen_random_uuid(), :o, :c, :d, 5000, 2000), "
        "(gen_random_uuid(), :o, :c, :d, 800, 0)",
        o=org["id"],
        c=str(acme.id),
        d=past,
    )
    await sql(
        "UPDATE payments SET voided_at = now() WHERE amount = 800 AND client_id = :c",
        c=str(acme.id),
    )
    return owner, org, acme


async def test_financial_snapshot(client):
    owner, org, acme = await _money_fixture(client)
    response = await client.get(org_url(org, f"/clients/{acme.id}"), headers=owner.headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["financials"] == {
        "wip_time_value": 18_000,
        "wip_disbursement_value": 1_300,
        "wip_total": 19_300,
        "unbilled_minutes": 90,
        "outstanding_balance": 16_000,
        "overdue_balance": 10_000,
        "on_account": 3_000,
    }


async def test_financial_snapshot_without_approval_counts_open_time(client):
    owner, org, acme = await _money_fixture(client)
    await sql(
        "UPDATE organizations SET require_timesheet_approval = false WHERE id = :o", o=org["id"]
    )
    data = (await client.get(org_url(org, f"/clients/{acme.id}"), headers=owner.headers)).json()
    assert data["data"]["financials"]["wip_time_value"] == 18_000 + 12_000
    assert data["data"]["financials"]["unbilled_minutes"] == 150


async def test_financial_snapshot_hidden_without_permissions(client):
    _owner, org, acme = await _money_fixture(client)
    member = await staff(org)
    data = (await client.get(org_url(org, f"/clients/{acme.id}"), headers=member.headers)).json()
    assert set(data["data"]["financials"].values()) == {None}
    billing = await staff(org, "billing")
    data = (await client.get(org_url(org, f"/clients/{acme.id}"), headers=billing.headers)).json()
    assert data["data"]["financials"]["wip_total"] == 19_300
    assert data["data"]["financials"]["on_account"] == 3_000


async def test_recurring_fees_are_annualised_with_vat_rounded_half_up(client):
    owner, org = await practice(client)
    reduced = await vat_rate_id(org, "reduced")
    created = await client.post(
        org_url(org, "/services"),
        headers=owner.headers,
        json={
            "code": "MGMT",
            "name": "Management accounts",
            "default_fee": 3333,
            "vat_rate_id": str(reduced),
        },
    )
    mgmt = created.json()["data"]["id"]
    acme = await create_client(client, owner, org)
    url = org_url(org, f"/clients/{acme['id']}/services")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    for body in (
        {
            "service_id": str(await service_id(org, "PAYROLL")),
            "fee": 12_345,
            "frequency": "monthly",
        },
        {"service_id": mgmt, "frequency": "quarterly"},
        {"service_id": str(await service_id(org, "ACCS")), "frequency": "one_off"},
        {
            "service_id": str(await service_id(org, "VAT")),
            "frequency": "quarterly",
            "is_active": False,
        },
        {"service_id": str(await service_id(org, "CT")), "end_date": yesterday},
    ):
        assert (await client.post(url, headers=owner.headers, json=body)).status_code == 201
    data = (await client.get(org_url(org, f"/clients/{acme['id']}"), headers=owner.headers)).json()
    fees = data["data"]["recurring_fees"]
    # 12,345 x 12 = 148,140 (VAT 20% = 29,628); 3,333 x 4 = 13,332 (VAT 5% = 666.6 -> 667).
    assert fees == {
        "annual_net": 161_472,
        "annual_vat": 30_295,
        "annual_gross": 191_767,
        "one_off_net": 120_000,
    }
    assert [s["code"] for s in data["data"]["services"]] == ["MGMT", "ACCS", "CT", "PAYROLL"]


async def test_recurring_fees_have_no_vat_when_not_vat_registered(client):
    owner, org = await practice(client, vat_registered=False)
    acme = await create_client(client, owner, org)
    await client.post(
        org_url(org, f"/clients/{acme['id']}/services"),
        headers=owner.headers,
        json={"service_id": str(await service_id(org, "VAT")), "frequency": "quarterly"},
    )
    data = (await client.get(org_url(org, f"/clients/{acme['id']}"), headers=owner.headers)).json()
    assert data["data"]["recurring_fees"]["annual_net"] == 60_000
    assert data["data"]["recurring_fees"]["annual_vat"] == 0


async def test_client_vat_default_applies_when_service_has_none(client):
    owner, org = await practice(client)
    zero = await vat_rate_id(org, "zero")
    books = await service_id(org, "BOOKS")
    await client.patch(
        org_url(org, f"/services/{books}"), headers=owner.headers, json={"vat_rate_id": None}
    )
    acme = await create_client(client, owner, org, default_vat_rate_id=str(zero))
    await client.post(
        org_url(org, f"/clients/{acme['id']}/services"),
        headers=owner.headers,
        json={"service_id": str(books), "fee": 1000, "frequency": "monthly"},
    )
    data = (await client.get(org_url(org, f"/clients/{acme['id']}"), headers=owner.headers)).json()
    assert data["data"]["recurring_fees"]["annual_vat"] == 0
    assert data["data"]["recurring_fees"]["annual_net"] == 12_000

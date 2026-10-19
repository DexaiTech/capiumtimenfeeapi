"""CSV client import: header mapping, dry run, all-or-nothing, limits."""

import pytest

from app.services.client_import import MAX_IMPORT_BYTES
from tests.clients_helpers import create_client, org_url, scalar, sql
from tests.domain_factories import practice, staff
from tests.factories import audit_entries, error


def csv_of(*lines: str) -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode()


async def upload(client, actor, org, content: bytes, *, dry_run: bool = False, **kwargs):
    query = "?dry_run=true" if dry_run else ""
    return await client.post(
        org_url(org, f"/imports/clients{query}"),
        headers=actor.headers,
        files={"file": ("clients.csv", content, "text/csv")},
        **kwargs,
    )


async def client_count(org: dict) -> int:
    return await scalar(
        "SELECT count(*) FROM clients WHERE organization_id = :o AND deleted_at IS NULL",
        o=org["id"],
    )


GOOD = csv_of(
    "Client Name,Code,Type,Reg No,Segment,AML,Health,Software,Contact,Contact Email,Services,Year End,Manager",
    'Acme Widgets Ltd,,Limited Company,SC123456,Gold,clear,80,Xero,Jane Doe,jane@acme.example,"VAT; payroll",31/03,mgr@example.com',
    "Bravo Partners,BRAVO,partnership,,Silver,pending,,QuickBooks,,,ct,,",
    "Charlie Sole,,Sole Trader,,,,,,Charlie,,,05/04/2026,",
)


async def test_dry_run_validates_without_writing(client):
    owner, org = await practice(client)
    await staff(org, "manager", email="mgr@example.com")
    response = await upload(client, owner, org, GOOD, dry_run=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["dry_run"] is True
    assert data["errors"] == []
    assert data["total_rows"] == 3 and data["valid_rows"] == 3 and data["created"] == 0
    assert [c["name"] for c in data["clients"]] == [
        "Acme Widgets Ltd",
        "Bravo Partners",
        "Charlie Sole",
    ]
    assert data["clients"][1]["code"] == "BRAVO"
    assert data["clients"][0]["code"] is None  # generated on the real run
    assert "manager_email" in data["columns"] and data["ignored_columns"] == []
    assert await client_count(org) == 0


async def test_real_import_creates_everything(client):
    owner, org = await practice(client)
    manager = await staff(org, "manager", email="mgr@example.com")
    response = await upload(client, owner, org, GOOD)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["created"] == 3
    assert [c["code"] for c in data["clients"]] == ["C0001", "BRAVO", "C0002"]
    assert all(c["id"] for c in data["clients"])

    rows = {
        r["name"]: r
        for r in await sql(
            "SELECT name, code, client_type, company_number, segment, aml_status, health_score, "
            "accounting_software, year_end_month, year_end_day, manager_user_id FROM clients "
            "WHERE organization_id = :o",
            o=org["id"],
        )
    }
    acme = rows["Acme Widgets Ltd"]
    assert acme["client_type"] == "limited_company"
    assert acme["company_number"] == "SC123456"
    assert acme["accounting_software"] == "xero"
    assert acme["health_score"] == 80
    assert (acme["year_end_day"], acme["year_end_month"]) == (31, 3)
    assert acme["manager_user_id"] == manager.id
    assert rows["Bravo Partners"]["accounting_software"] == "quickbooks"
    assert (rows["Charlie Sole"]["year_end_day"], rows["Charlie Sole"]["year_end_month"]) == (5, 4)

    listed = (await client.get(org_url(org, "/clients"), headers=owner.headers)).json()["data"]
    by_name = {c["name"]: c for c in listed}
    assert by_name["Acme Widgets Ltd"]["contact_name"] == "Jane Doe"
    assert by_name["Acme Widgets Ltd"]["contact_email"] == "jane@acme.example"
    assert sorted(s["code"] for s in by_name["Acme Widgets Ltd"]["services"]) == ["PAYROLL", "VAT"]
    assert [s["code"] for s in by_name["Bravo Partners"]["services"]] == ["CT"]
    assert by_name["Charlie Sole"]["primary_contact"]["name"] == "Charlie"
    audit = (await audit_entries(org["id"], "client.imported"))[0]["metadata"]
    assert audit["count"] == 3 and audit["codes"] == ["C0001", "BRAVO", "C0002"]


BAD = csv_of(
    "name,code,email,client_type,services,manager_email,contact_email,health_score",
    "Good One,DUP,,,,,,",
    "Dup Code,dup,,,,,,",
    "Taken Code,TAKEN,,,,,,",
    "Bad Email,,not-an-email,,,,,",
    "Bad Type,,,plc,,,,",
    "Bad Service,,,,NOPE,,,",
    "Bad Manager,,,,,ghost@example.com,,",
    "Orphan Contact Email,,,,,,x@y.io,",
    ",NONAME,,,,,,",
    "Too Healthy,,,,,,,101",
)


async def test_dry_run_reports_every_row_error(client):
    owner, org = await practice(client)
    await create_client(client, owner, org, "Existing", code="TAKEN")
    response = await upload(client, owner, org, BAD, dry_run=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    errors = {(e["row"], e["field"]) for e in data["errors"]}
    assert errors == {
        (2, "code"),
        (3, "code"),
        (4, "email"),
        (5, "client_type"),
        (6, "services"),
        (7, "manager_email"),
        (8, "contact_name"),
        (9, "name"),
        (10, "health_score"),
    }
    taken = next(e for e in data["errors"] if e["row"] == 3)
    assert taken["code"] == "CLIENT_CODE_TAKEN"
    assert data["valid_rows"] == 1 and data["total_rows"] == 10
    assert await client_count(org) == 1


async def test_real_run_with_errors_writes_nothing(client):
    owner, org = await practice(client)
    await create_client(client, owner, org, "Existing", code="TAKEN")
    response = await upload(client, owner, org, BAD)
    assert response.status_code == 422
    body = error(response)
    assert body["code"] == "IMPORT_INVALID"
    assert len(body["details"]) == 9
    assert await client_count(org) == 1
    seq = await scalar(
        "SELECT next_number FROM document_sequences WHERE organization_id = :o AND kind = 'client'",
        o=org["id"],
    )
    assert seq == 1


async def test_generated_codes_skip_codes_in_the_file(client):
    owner, org = await practice(client)
    content = csv_of("name,code", "First,", "Second,C0001", "Third,")
    data = (await upload(client, owner, org, content)).json()["data"]
    assert [c["code"] for c in data["clients"]] == ["C0002", "C0001", "C0003"]


async def test_raw_csv_body_and_windows_1252_are_accepted(client):
    owner, org = await practice(client)
    content = "name,city\r\nCafé Crème Ltd,Zürich\r\n".encode("cp1252")
    response = await client.post(
        org_url(org, "/imports/clients"),
        headers={**owner.headers, "Content-Type": "text/csv"},
        content=content,
    )
    assert response.status_code == 200, response.text
    assert (
        await scalar("SELECT city FROM clients WHERE organization_id = :o", o=org["id"]) == "Zürich"
    )


async def test_utf8_bom_and_unknown_columns(client):
    owner, org = await practice(client)
    content = "﻿Name,Favourite Colour\r\nAcme,Blue\r\n".encode()
    data = (await upload(client, owner, org, content)).json()["data"]
    assert data["created"] == 1
    assert data["ignored_columns"] == ["Favourite Colour"]


@pytest.mark.parametrize(
    ("content", "status", "code"),
    [
        (csv_of("code,email", "A,a@b.io"), 422, "IMPORT_MISSING_COLUMNS"),
        (csv_of("name,client name", "A,B"), 422, "IMPORT_DUPLICATE_COLUMNS"),
        (b"", 400, "FILE_INVALID"),
        (csv_of("name"), 400, "FILE_INVALID"),
        (b"\xff\xfe\x00\x81\x8d\x8f\x90\x9d", 400, "FILE_INVALID"),
    ],
)
async def test_file_level_errors(client, content, status, code):
    owner, org = await practice(client)
    response = await upload(client, owner, org, content)
    assert response.status_code == status, response.text
    assert error(response)["code"] == code


async def test_more_than_5000_rows_is_rejected(client):
    owner, org = await practice(client)
    content = csv_of("name", *(f"Client {i}" for i in range(5001)))
    response = await upload(client, owner, org, content, dry_run=True)
    assert response.status_code == 422
    assert error(response)["code"] == "IMPORT_TOO_MANY_ROWS"


async def test_5000_rows_import_in_one_go(client):
    owner, org = await practice(client)
    content = csv_of("name,contact_name", *(f"Client {i},Person {i}" for i in range(5000)))
    response = await upload(client, owner, org, content)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["created"] == 5000
    assert await client_count(org) == 5000
    assert (
        await scalar(
            "SELECT count(*) FROM client_contacts WHERE organization_id = :o AND is_primary",
            o=org["id"],
        )
        == 5000
    )


async def test_file_over_5_mb_is_413(client):
    owner, org = await practice(client)
    filler = "x" * 900
    lines = ["name,notes"] + [f"C{i},{filler}" for i in range(MAX_IMPORT_BYTES // 900 + 10)]
    response = await upload(client, owner, org, csv_of(*lines))
    assert response.status_code == 413
    assert error(response)["code"] == "FILE_TOO_LARGE"


async def test_wrong_content_type_and_missing_file_field(client):
    owner, org = await practice(client)
    response = await client.post(
        org_url(org, "/imports/clients"), headers=owner.headers, json={"name": "A"}
    )
    assert response.status_code == 415
    response = await client.post(
        org_url(org, "/imports/clients"),
        headers=owner.headers,
        files={"upload": ("c.csv", b"name\r\nA\r\n", "text/csv")},
    )
    assert response.status_code == 400
    assert error(response)["code"] == "FILE_INVALID"


async def test_import_permissions(client):
    owner, org = await practice(client)
    _, other = await practice(client, "Other")
    content = csv_of("name,status", "Old Client,archived")
    for role in ("staff", "billing", "viewer"):
        actor = await staff(org, role)
        assert (await upload(client, actor, org, content, dry_run=True)).status_code == 403
    manager = await staff(org, "manager")
    data = (await upload(client, manager, org, content, dry_run=True)).json()["data"]
    assert data["errors"][0]["field"] == "status"
    assert (await upload(client, owner, org, content)).status_code == 200
    outsider = await staff(other, "owner")
    assert (await upload(client, outsider, org, content)).status_code == 404

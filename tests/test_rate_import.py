"""CSV rate import and export: levels, dry run, overlaps, close_previous, masking, permissions."""

import pytest

from tests.clients_helpers import create_client, org_url, scalar, sql
from tests.domain_factories import practice, staff
from tests.factories import audit_entries, error
from tests.import_export_helpers import csv_of, export_rows, role_with, upload
from tests.job_helpers import role_without
from tests.team_helpers import new_rate, new_staff

HEADER = "Kind,Staff Code,Grade,Client Code,Service,Rate,From,To"


async def rule_count(org: dict) -> int:
    return await scalar("SELECT count(*) FROM rate_rules WHERE organization_id = :o", o=org["id"])


async def setup(client):
    owner, org = await practice(client)
    await new_staff(client, owner, org, "Wanda Worker", code="WW")
    await create_client(client, owner, org, "Acme Ltd", code="ACME")
    return owner, org


GOOD = csv_of(
    HEADER,
    "cost,WW,,,,18.50,01/01/2026,",
    "charge,WW,,,,95,2026-01-01,",
    "charge,,SNR,,,110.00,2026-01-01,2026-12-31",
    "cost,,Senior,,,30,2026-01-01,",
    "charge,,,ACME,,120,2026-01-01,",
    "charge,,JNR,,ACCS,70,2026-01-01,",
    "charge,,,ACME,VAT,130,2026-01-01,",
    "charge,WW,,ACME,,99.5,2026-02-01,",
    'charge,WW,,ACME,ACCS,"£1,000.00",2026-02-01,',
)


async def test_dry_run_validates_without_writing(client):
    owner, org = await setup(client)
    response = await upload(client, owner, org, "rates", GOOD, dry_run=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["errors"] == [] and data["total_rows"] == 9 and data["valid_rows"] == 9
    assert data["created"] == 0 and data["counts"] == {"cost": 2, "charge": 7}
    assert data["items"][0]["name"] == "cost 18.50 for WW from 2026-01-01"
    assert all(i["id"] is None and i["code"] is None for i in data["items"])
    assert await rule_count(org) == 0
    assert (
        await scalar("SELECT count(*) FROM rate_changes WHERE organization_id = :o", o=org["id"])
        == 0
    )


async def test_real_import_creates_every_level(client):
    owner, org = await setup(client)
    response = await upload(client, owner, org, "rates", GOOD)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["created"] == 9 and all(i["id"] for i in data["items"])
    rules = await sql(
        "SELECT kind, level, rate, effective_from, effective_to FROM rate_rules "
        "WHERE organization_id = :o ORDER BY created_at, id",
        o=org["id"],
    )
    assert [(r["kind"], r["level"], r["rate"]) for r in rules] == [
        ("cost", "staff", 1850),
        ("charge", "staff", 9500),
        ("charge", "grade", 11000),
        ("cost", "grade", 3000),
        ("charge", "client", 12000),
        ("charge", "service_grade", 7000),
        ("charge", "client_service", 13000),
        ("charge", "client_staff", 9950),
        ("charge", "client_service_staff", 100000),
    ]
    assert str(rules[2]["effective_to"]) == "2026-12-31"
    assert str(rules[0]["effective_from"]) == "2026-01-01"
    audit = (await audit_entries(org["id"], "rate.imported"))[0]
    assert audit["metadata"] == {"count": 9, "cost": 2, "charge": 7, "close_previous": False}
    assert audit["target_type"] == "rate_rule" and audit["actor_user_id"] == str(owner.id)
    assert await audit_entries(org["id"], "rate.created") == []
    changes = await scalar(
        "SELECT count(*) FROM rate_changes WHERE organization_id = :o AND source = 'import' "
        "AND action = 'created'",
        o=org["id"],
    )
    assert changes == 9
    listed = (await client.get(org_url(org, "/rates?limit=100"), headers=owner.headers)).json()
    assert len(listed["data"]) == 9


BAD = csv_of(
    "kind,staff_code,grade,client_code,service_code,rate,effective_from,effective_to",
    "charge,WW,,,,50,2026-01-01,",
    "charge,WW,,,,60,2026-06-01,",
    "charge,GHOST,,,,50,2026-01-01,",
    "charge,,NOPE,,,50,2026-01-01,",
    "charge,,,NOCLIENT,,50,2026-01-01,",
    "charge,,,,NOSERVICE,50,2026-01-01,",
    "charge,WW,SNR,,,50,2026-01-01,",
    "cost,,,ACME,,50,2026-01-01,",
    "bonus,WW,,,,50,2026-01-01,",
    "charge,,JNR,,,-5,2026-01-01,",
    "charge,,JNR,,,5.123,2026-01-01,",
    "charge,,JNR,,,abc,2026-01-01,",
    "charge,,JNR,,,50,2026-13-01,",
    "charge,,JNR,,,50,2026-03-01,2026-02-01",
    "charge,,,,,50,2026-01-01,",
    ",,JNR,,,50,2026-01-01,",
)


async def test_dry_run_reports_every_row_error(client):
    owner, org = await setup(client)
    response = await upload(client, owner, org, "rates", BAD, dry_run=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    errors = {(e["row"], e["field"]): e["code"] for e in data["errors"]}
    assert errors == {
        (2, "effective_from"): "RATE_OVERLAP",
        (3, "staff_code"): "unknown_staff",
        (4, "grade"): "unknown_grade",
        (5, "client_code"): "unknown_client",
        (6, "service_code"): "unknown_service",
        (7, "row"): "invalid_scope",
        (8, "kind"): "invalid_kind",
        (9, "kind"): "invalid",
        (10, "rate"): "invalid",
        (11, "rate"): "invalid",
        (12, "rate"): "invalid",
        (13, "effective_from"): "invalid",
        (14, "effective_to"): "invalid",
        (15, "row"): "invalid_scope",
        (16, "kind"): "missing",
    }
    assert data["total_rows"] == 16 and data["valid_rows"] == 1
    assert await rule_count(org) == 0


async def test_real_run_with_errors_writes_nothing(client):
    owner, org = await setup(client)
    response = await upload(client, owner, org, "rates", BAD)
    assert response.status_code == 422
    body = error(response)
    assert body["code"] == "IMPORT_INVALID" and len(body["details"]) == 15
    assert await rule_count(org) == 0
    assert await audit_entries(org["id"], "rate.imported") == []
    assert (
        await scalar("SELECT count(*) FROM rate_changes WHERE organization_id = :o", o=org["id"])
        == 0
    )


async def test_existing_rule_overlap_and_close_previous(client):
    owner, org = await setup(client)
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=str(await _staff_id(org, "WW")),
        rate=8000,
        effective_from="2026-01-01",
    )
    content = csv_of(HEADER, "charge,WW,,,,90,2026-07-01,")
    dry = (await upload(client, owner, org, "rates", content, dry_run=True)).json()["data"]
    assert dry["errors"][0]["code"] == "RATE_OVERLAP"
    assert (await upload(client, owner, org, "rates", content)).status_code == 422
    response = await upload(client, owner, org, "rates", content, close_previous=True)
    assert response.status_code == 200, response.text
    rows = await sql(
        "SELECT rate, effective_from, effective_to FROM rate_rules WHERE organization_id = :o "
        "ORDER BY effective_from",
        o=org["id"],
    )
    assert [
        (r["rate"], str(r["effective_from"]), r["effective_to"] and str(r["effective_to"]))
        for r in rows
    ] == [
        (8000, "2026-01-01", "2026-06-30"),
        (9000, "2026-07-01", None),
    ]
    assert (await audit_entries(org["id"], "rate.imported"))[0]["metadata"][
        "close_previous"
    ] is True
    # Within one file a later row may close an earlier one too.
    both = csv_of(HEADER, "charge,WW,,,,95,2026-09-01,", "charge,WW,,,,99,2026-10-01,")
    assert (await upload(client, owner, org, "rates", both)).status_code == 422
    assert (await upload(client, owner, org, "rates", both, close_previous=True)).status_code == 200


async def _staff_id(org: dict, code: str):
    return await scalar(
        "SELECT user_id FROM organization_members WHERE organization_id = :o AND staff_code = :c",
        o=org["id"],
        c=code,
    )


async def test_aliases_unknown_columns_and_file_errors(client):
    owner, org = await setup(client)
    content = "﻿Type,Grade Name,Amount,Valid From,Notes\r\ncost,Junior,12.5,1/2/2026,hi\r\n".encode()
    data = (await upload(client, owner, org, "rates", content)).json()["data"]
    assert data["created"] == 1 and data["ignored_columns"] == ["Notes"]
    assert data["columns"] == ["kind", "grade", "rate", "effective_from"]
    for body, status, code in (
        (csv_of("kind,rate", "cost,5"), 422, "IMPORT_MISSING_COLUMNS"),
        (
            csv_of("kind,type,rate,effective_from", "cost,cost,5,2026-01-01"),
            422,
            "IMPORT_DUPLICATE_COLUMNS",
        ),
        (csv_of("kind,rate,effective_from"), 400, "FILE_INVALID"),
        (b"", 400, "FILE_INVALID"),
    ):
        response = await upload(client, owner, org, "rates", body)
        assert response.status_code == status, response.text
        assert error(response)["code"] == code
    big = csv_of("kind,grade,rate,effective_from", *("cost,JNR,1,2026-01-01" for _ in range(5001)))
    response = await upload(client, owner, org, "rates", big, dry_run=True)
    assert error(response)["code"] == "IMPORT_TOO_MANY_ROWS"
    response = await client.post(org_url(org, "/imports/rates"), headers=owner.headers, json={})
    assert response.status_code == 415


async def test_import_permissions(client):
    owner, org = await setup(client)
    _, other = await practice(client, "Other")
    content = csv_of(HEADER, "charge,,JNR,,,50,2026-01-01,")
    for actor in (
        await staff(org, "manager"),
        await staff(org, "staff"),
        await role_without(org, "data.import_export"),
        await role_with(org, "data.import_export", "rate.read"),
        await role_with(org, "rate.manage"),
    ):
        assert (await upload(client, actor, org, "rates", content, dry_run=True)).status_code == 403
    manager = await role_with(org, "data.import_export", "rate.manage")
    assert (await upload(client, manager, org, "rates", content)).status_code == 200
    assert (
        await upload(client, await staff(other, "owner"), org, "rates", content)
    ).status_code == 404
    assert owner is not None


async def test_export_lists_every_rule_with_status_and_guards_formulas(client):
    owner = org = None
    owner, org = await practice(client)
    await new_staff(client, owner, org, "=HYPERLINK(1) Wanda", code="WW")
    await create_client(client, owner, org, "@evil Ltd", code="EV")
    content = csv_of(
        HEADER,
        "cost,WW,,,,18.5,2026-01-01,",
        "charge,WW,,,,95,2020-01-01,2020-12-31",
        "charge,,SNR,,,110,2099-01-01,",
        "charge,,,EV,,120,2020-01-01,",
        "charge,,JNR,,ACCS,70,2020-01-01,",
    )
    assert (await upload(client, owner, org, "rates", content)).status_code == 200
    response, rows = await export_rows(client, owner, org, "rates")
    assert response.status_code == 200 and "rates.csv" in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert len(rows) == 5
    by = {(r["kind"], r["level"]): r for r in rows}
    assert by[("charge", "Client (everyone)")]["client_name"] == "'@evil Ltd"
    assert by[("cost", "Staff")]["staff_name"] == "'=HYPERLINK(1) Wanda"
    assert by[("cost", "Staff")]["rate"] == "18.50" and by[("cost", "Staff")]["status"] == "current"
    assert (
        by[("charge", "Grade")]["status"] == "future" and by[("charge", "Grade")]["grade"] == "SNR"
    )
    assert by[("charge", "Service + grade")]["service_code"] == "ACCS"
    assert {r["status"] for r in rows if r["effective_to"]} == {"ended"}
    assert rows[0]["kind"] == "charge" and rows[-1]["kind"] == "cost"
    # What the export writes the import reads back (the extra columns are ignored).
    owner2, org2 = await practice(client, "Second")
    await new_staff(client, owner2, org2, "Wanda", code="WW")
    await create_client(client, owner2, org2, "Evil", code="EV")
    again = await upload(client, owner2, org2, "rates", response.content)
    assert again.status_code == 200, again.text
    assert again.json()["data"]["created"] == 5
    assert again.json()["data"]["ignored_columns"] == [
        "level",
        "status",
        "staff_name",
        "client_name",
        "service_name",
    ]


async def test_export_permissions(client):
    owner, org = await setup(client)
    await upload(
        client,
        owner,
        org,
        "rates",
        csv_of(HEADER, "cost,WW,,,,18.5,2026-01-01,", "charge,WW,,,,95,2026-01-01,"),
    )
    reader = await role_with(org, "data.import_export", "rate.read")
    _, rows = await export_rows(client, reader, org, "rates")
    assert [r["kind"] for r in rows] == ["charge", "cost"]  # rate.read shows costs, as GET /rates
    for actor in (
        await staff(org, "manager"),
        await role_with(org, "data.import_export"),
        await role_with(org, "rate.read"),
        await role_with(org, "data.import_export", "rate.manage"),
    ):
        assert (await export_rows(client, actor, org, "rates"))[0].status_code == 403


@pytest.mark.parametrize("kind", ["staff", "rates", "jobs"])
async def test_export_routes_are_not_read_as_ids(client, kind):
    owner, org = await practice(client)
    response = await client.get(org_url(org, f"/{kind}/export"), headers=owner.headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")

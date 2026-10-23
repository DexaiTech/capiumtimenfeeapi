"""CSV job import and export: columns, defaults from the service, dry run, rollback, permissions."""

from tests.clients_helpers import create_client, org_url, scalar, sql
from tests.domain_factories import practice, staff
from tests.factories import audit_entries, error
from tests.import_export_helpers import csv_of, export_rows, role_with, upload
from tests.job_helpers import create_job, role_without
from tests.team_helpers import new_staff

HEADER = (
    "Job Name,Client,Service,Status,Basis,Fixed Fee,Rate,Budget Hours,Budget Fee,Period From,"
    "Period To,Start,Due,Manager,Partner,Assignees,Notes"
)


async def job_count(org: dict) -> int:
    return await scalar(
        "SELECT count(*) FROM jobs WHERE organization_id = :o AND deleted_at IS NULL", o=org["id"]
    )


async def setup(client):
    owner, org = await practice(client)
    await create_client(client, owner, org, "Acme Ltd", code="ACME")
    await create_client(client, owner, org, "Bravo Ltd", code="BRAVO")
    await new_staff(client, owner, org, "Wanda Worker", code="WW", email="ww@firm.example")
    await new_staff(client, owner, org, "Xavier", code="XX")
    return owner, org


GOOD = csv_of(
    HEADER,
    "Year end 2026,ACME,ACCS,in progress,,,,12.5,2500.00,01/04/2025,31/03/2026,2026-04-01,2026-12-31,"
    "WW,XX,WW;xx,Prepare the accounts",
    "VAT Q1,bravo,VAT,,,,,,,,,,,ww@firm.example,,,",
    "Ad hoc advice,ACME,,,Non-billable,,,,,,,,,,,,",
    "Retainer,BRAVO,,,fixed,1200,95.50,,,,,,,,,,",
)


async def test_dry_run_validates_without_writing(client):
    owner, org = await setup(client)
    response = await upload(client, owner, org, "jobs", GOOD, dry_run=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["errors"] == [] and data["total_rows"] == 4 and data["valid_rows"] == 4
    assert data["created"] == 0
    assert [i["name"] for i in data["items"]] == [
        "Year end 2026",
        "VAT Q1",
        "Ad hoc advice",
        "Retainer",
    ]
    assert [i["code"] for i in data["items"]] == ["J00001", "J00002", "J00003", "J00004"]
    assert all(i["id"] is None for i in data["items"])
    assert await job_count(org) == 0
    assert (
        await scalar(
            "SELECT next_number FROM document_sequences WHERE organization_id = :o AND kind = 'job'",
            o=org["id"],
        )
        == 1
    )


async def test_real_import_creates_jobs_like_post_jobs(client):
    owner, org = await setup(client)
    response = await upload(client, owner, org, "jobs", GOOD)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["created"] == 4 and all(i["id"] for i in data["items"])
    listed = (
        await client.get(org_url(org, "/jobs?limit=100&sort=number"), headers=owner.headers)
    ).json()["data"]
    by_name = {j["name"]: j for j in listed}
    year_end = by_name["Year end 2026"]
    assert year_end["number"] == "J00001" and year_end["status"] == "in_progress"
    assert year_end["client_code"] == "ACME" and year_end["service_name"] == "Year-end accounts"
    assert (
        year_end["billing_basis"] == "fixed" and year_end["fixed_fee"] == 120_000
    )  # service default
    assert year_end["budget_minutes"] == 750 and year_end["budget_fee"] == 250_000
    assert (year_end["period_start"], year_end["period_end"]) == ("2025-04-01", "2026-03-31")
    assert (year_end["start_date"], year_end["due_date"]) == ("2026-04-01", "2026-12-31")
    assert year_end["description"] == "Prepare the accounts"
    assert len(year_end["assignee_user_ids"]) == 2
    ww = await scalar(
        "SELECT user_id FROM organization_members WHERE organization_id = :o AND staff_code = 'WW'",
        o=org["id"],
    )
    assert year_end["manager_user_id"] == str(ww)
    assert by_name["VAT Q1"]["manager_user_id"] == str(ww)  # by email
    assert by_name["VAT Q1"]["status"] == "planned" and by_name["VAT Q1"]["client_code"] == "BRAVO"
    assert by_name["Ad hoc advice"]["billing_basis"] == "non_billable"
    assert (
        by_name["Retainer"]["fixed_fee"] == 120_000 and by_name["Retainer"]["hourly_rate"] == 9550
    )
    audit = (await audit_entries(org["id"], "job.imported"))[0]
    assert audit["metadata"]["count"] == 4 and audit["metadata"]["codes"][0] == "J00001"
    assert audit["target_type"] == "job"
    assert len(await audit_entries(org["id"], "job.created")) == 4


BAD = csv_of(
    "name,client_code,service_code,status,billing_basis,fixed_fee,budget_hours,start_date,due_date,"
    "manager,assignees,period_start,period_end",
    "Good,ACME,,,,,,,,,,,",
    ",ACME,,,,,,,,,,,",
    "No Client,,,,,,,,,,,,",
    "Ghost Client,NOPE,,,,,,,,,,,",
    "Ghost Service,ACME,NOPE,,,,,,,,,,",
    "Bad Status,ACME,,closed,,,,,,,,,",
    "Bad Basis,ACME,,,weekly,,,,,,,,",
    "Fixed No Fee,ACME,,,fixed,,,,,,,,",
    "Bad Fee,ACME,,,,-3,,,,,,,",
    "Bad Hours,ACME,,,,,abc,,,,,,",
    "Bad Dates,ACME,,,,,,2026-05-01,2026-04-01,,,,",
    "Bad Date,ACME,,,,,,31/02/2026,,,,,",
    "Ghost Manager,ACME,,,,,,,,nobody@x.io,,,",
    "Ghost Assignee,ACME,,,,,,,,,WW;ZZ,,",
    "Bad Period,ACME,,,,,,,,,,2026-05-01,2026-04-01",
)


async def test_dry_run_reports_every_row_error(client):
    owner, org = await setup(client)
    response = await upload(client, owner, org, "jobs", BAD, dry_run=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    errors = {(e["row"], e["field"]): e["code"] for e in data["errors"]}
    assert errors.keys() == {
        (2, "name"),
        (3, "client_code"),
        (4, "client_code"),
        (5, "service_code"),
        (6, "status"),
        (7, "billing_basis"),
        (8, "fixed_fee"),
        (9, "fixed_fee"),
        (10, "budget_hours"),
        (11, "due_date"),
        (12, "start_date"),
        (13, "manager"),
        (14, "assignees"),
        (15, "period_end"),
    }
    assert (
        errors[(3, "client_code")] == "missing" and errors[(4, "client_code")] == "unknown_client"
    )
    assert data["total_rows"] == 15 and data["valid_rows"] == 1
    assert await job_count(org) == 0


async def test_real_run_with_errors_writes_nothing(client):
    owner, org = await setup(client)
    response = await upload(client, owner, org, "jobs", BAD)
    assert response.status_code == 422
    body = error(response)
    assert body["code"] == "IMPORT_INVALID" and len(body["details"]) == 14
    assert await job_count(org) == 0
    assert await audit_entries(org["id"], "job.imported") == []
    assert await audit_entries(org["id"], "job.created") == []
    assert (
        await scalar(
            "SELECT next_number FROM document_sequences WHERE organization_id = :o AND kind = 'job'",
            o=org["id"],
        )
        == 1
    )


async def test_domain_errors_of_the_create_rules_are_row_errors(client):
    owner, org = await setup(client)
    await sql(
        "UPDATE clients SET status = 'inactive' WHERE organization_id = :o AND code = 'BRAVO'",
        o=org["id"],
    )
    await sql(
        "UPDATE services SET is_active = false WHERE organization_id = :o AND code = 'PAYROLL'",
        o=org["id"],
    )
    content = csv_of(
        "name,client_code,service_code",
        "Fine,ACME,",
        "Inactive client,BRAVO,",
        "Inactive service,ACME,PAYROLL",
    )
    data = (await upload(client, owner, org, "jobs", content, dry_run=True)).json()["data"]
    assert {(e["row"], e["field"]) for e in data["errors"]} == {(2, "row"), (3, "service_code")}
    assert data["errors"][0]["code"] == "CLIENT_INACTIVE" and data["valid_rows"] == 1


async def test_aliases_unknown_columns_and_file_errors(client):
    owner, org = await setup(client)
    content = "﻿Job,Client Ref,Number,Deadline\r\nTax return,ACME,J99,05/04/2027\r\n".encode()
    data = (await upload(client, owner, org, "jobs", content)).json()["data"]
    assert data["created"] == 1 and data["ignored_columns"] == ["Number"]
    assert data["items"][0]["code"] == "J00001"  # numbers always come from the sequence
    for body, status, code in (
        (csv_of("name,service_code", "A,VAT"), 422, "IMPORT_MISSING_COLUMNS"),
        (csv_of("name,job,client_code", "A,B,ACME"), 422, "IMPORT_DUPLICATE_COLUMNS"),
        (csv_of("name,client_code"), 400, "FILE_INVALID"),
    ):
        response = await upload(client, owner, org, "jobs", body)
        assert response.status_code == status, response.text
        assert error(response)["code"] == code
    big = csv_of("name,client_code", *("J,ACME" for _ in range(5001)))
    assert (
        error(await upload(client, owner, org, "jobs", big, dry_run=True))["code"]
        == "IMPORT_TOO_MANY_ROWS"
    )
    response = await client.post(org_url(org, "/imports/jobs"), headers=owner.headers, json={})
    assert response.status_code == 415


async def test_import_permissions(client):
    _, org = await setup(client)
    _, other = await practice(client, "Other")
    content = csv_of("name,client_code", "Tax return,ACME")
    for actor in (
        await staff(org, "manager"),  # job.create but not data.import_export
        await staff(org, "staff"),
        await role_without(org, "data.import_export"),
        await role_with(org, "data.import_export", "job.read"),
        await role_with(org, "job.create", "client.read"),
    ):
        assert (await upload(client, actor, org, "jobs", content, dry_run=True)).status_code == 403
    allowed = await role_with(org, "data.import_export", "job.create")
    assert (await upload(client, allowed, org, "jobs", content)).status_code == 200
    admin = await staff(org, "admin")
    assert (await upload(client, admin, org, "jobs", content)).status_code == 200
    assert (
        await upload(client, await staff(other, "owner"), org, "jobs", content)
    ).status_code == 404


async def test_export_columns_formula_guard_and_round_trip(client):
    owner, org = await setup(client)
    acme = await scalar(
        "SELECT id FROM clients WHERE organization_id = :o AND code = 'ACME'", o=org["id"]
    )
    await create_job(client, owner, org, acme, "=1+1 Evil", description="-note")
    deleted = await create_job(client, owner, org, acme, "Gone")
    assert (
        await client.delete(org_url(org, f"/jobs/{deleted['id']}"), headers=owner.headers)
    ).status_code in (200, 204)
    assert (await upload(client, owner, org, "jobs", GOOD)).status_code == 200
    response, rows = await export_rows(client, owner, org, "jobs")
    assert response.status_code == 200 and "jobs.csv" in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert [r["number"] for r in rows] == ["J00001", "J00003", "J00004", "J00005", "J00006"][
        : len(rows)
    ]
    assert len(rows) == 5
    names = [r["name"] for r in rows]
    assert "'=1+1 Evil" in names and "Gone" not in names
    evil = rows[0]
    assert evil["description"] == "'-note" and evil["client_code"] == "ACME"
    year_end = next(r for r in rows if r["name"] == "Year end 2026")
    assert year_end["service_code"] == "ACCS" and year_end["status"] == "in_progress"
    assert year_end["fixed_fee"] == "1200.00" and year_end["budget_hours"] == "12.5"
    assert year_end["budget_fee"] == "2500.00" and year_end["manager"] == "WW"
    assert year_end["partner"] == "XX" and year_end["assignees"] in ("WW;XX", "XX;WW")
    assert year_end["due_date"] == "2026-12-31" and year_end["client_name"] == "Acme Ltd"
    # The first columns are what the import reads: the file imports into a fresh practice.
    owner2, org2 = await practice(client, "Second")
    await create_client(client, owner2, org2, "Acme Ltd", code="ACME")
    await create_client(client, owner2, org2, "Bravo Ltd", code="BRAVO")
    await new_staff(client, owner2, org2, "Wanda", code="WW")
    await new_staff(client, owner2, org2, "Xavier", code="XX")
    again = await upload(client, owner2, org2, "jobs", response.content, dry_run=True)
    assert again.status_code == 200, again.text
    data = again.json()["data"]
    assert data["errors"] == [], data["errors"]
    assert data["ignored_columns"] == ["number", "client_name"]


async def test_export_permissions(client):
    _, org = await setup(client)
    for actor in (
        await staff(org, "manager"),
        await role_with(org, "data.import_export"),
        await role_with(org, "job.read"),
    ):
        assert (await export_rows(client, actor, org, "jobs"))[0].status_code == 403
    reader = await role_with(org, "data.import_export", "job.read")
    assert (await export_rows(client, reader, org, "jobs"))[0].status_code == 200


async def test_a_failing_row_after_valid_rows_rolls_the_whole_file_back(client):
    owner, org = await setup(client)
    await sql(
        "UPDATE clients SET status = 'inactive' WHERE organization_id = :o AND code = 'BRAVO'",
        o=org["id"],
    )
    content = csv_of("name,client_code", "First,ACME", "Second,ACME", "Third,BRAVO")
    response = await upload(client, owner, org, "jobs", content)
    assert response.status_code == 422
    assert [d["row"] for d in error(response)["details"]] == [3]
    assert await job_count(org) == 0
    assert (
        await scalar(
            "SELECT next_number FROM document_sequences WHERE organization_id = :o AND kind = 'job'",
            o=org["id"],
        )
        == 1
    )

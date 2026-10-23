"""CSV staff import and export: columns, dry run, all-or-nothing, rates, permissions."""

import pytest

from tests.clients_helpers import org_url, scalar, sql
from tests.domain_factories import practice, staff
from tests.factories import audit_entries, error
from tests.import_export_helpers import csv_of, export_rows, role_with, upload
from tests.job_helpers import role_without
from tests.team_helpers import new_staff


async def staff_count(org: dict) -> int:
    return await scalar(
        "SELECT count(*) FROM organization_members WHERE organization_id = :o", o=org["id"]
    )


GOOD = csv_of(
    "Staff name,Staff code,Grade,Work email,Job title,Initials,Start,Hours per day,Days,Target,Approver,"
    "Cost rate,Charge rate,Rate from",
    "Ann Adams,,Senior,ann@firm.example,Tax senior,AA,01/02/2026,7.5,Mon-Thu,70%,,18.50,95,",
    "Bob Brown,BOB,JNR,,,BB,2026-02-02,,,,,,,",
    "Cy Clark,,Partner,,,,2026-03-01,,1;2;3;4;5,,,,150.00,2026-04-01",
)


async def test_dry_run_validates_without_writing(client):
    owner, org = await practice(client)
    before = await staff_count(org)
    seq_sql = (
        "SELECT next_number FROM document_sequences WHERE organization_id = :o AND kind = 'staff'"
    )
    seq_before = await scalar(seq_sql, o=org["id"])
    response = await upload(client, owner, org, "staff", GOOD, dry_run=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["dry_run"] is True and data["errors"] == []
    assert data["total_rows"] == 3 and data["valid_rows"] == 3 and data["created"] == 0
    assert [i["name"] for i in data["items"]] == ["Ann Adams", "Bob Brown", "Cy Clark"]
    assert data["items"][1]["code"] == "BOB" and data["items"][0]["code"]
    assert all(i["id"] is None for i in data["items"])
    assert data["counts"] == {"rates": 3}
    assert data["ignored_columns"] == []
    assert "approver_code" in data["columns"] and "full_name" in data["columns"]
    assert await staff_count(org) == before
    assert (
        await scalar("SELECT count(*) FROM rate_rules WHERE organization_id = :o", o=org["id"]) == 0
    )
    assert await scalar(seq_sql, o=org["id"]) == seq_before


async def test_real_import_creates_staff_and_rates(client):
    owner, org = await practice(client)
    response = await upload(client, owner, org, "staff", GOOD)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["created"] == 3 and data["counts"] == {"rates": 3}
    codes = [i["code"] for i in data["items"]]
    assert codes[1] == "BOB" and all(i["id"] for i in data["items"])

    listed = (await client.get(org_url(org, "/staff?limit=100"), headers=owner.headers)).json()[
        "data"
    ]
    by_name = {s["full_name"]: s for s in listed}
    ann = by_name["Ann Adams"]
    assert ann["grade"]["code"] == "SNR" and ann["email"] == "ann@firm.example"
    assert ann["working_minutes_per_day"] == 450 and ann["working_days"] == [1, 2, 3, 4]
    assert ann["chargeable_target_bp"] == 7000 and ann["initials"] == "AA"
    assert ann["start_date"] == "2026-02-01" and ann["has_login"] is False
    assert ann["cost_rate"] == 1850 and ann["charge_rate"] == 9500
    assert by_name["Bob Brown"]["code"] == "BOB"
    assert by_name["Bob Brown"]["working_days"] == [1, 2, 3, 4, 5]
    cy = by_name["Cy Clark"]
    assert cy["working_days"] == [1, 2, 3, 4, 5]
    rates = await sql(
        "SELECT kind, level, rate, effective_from FROM rate_rules WHERE organization_id = :o "
        "ORDER BY kind, rate",
        o=org["id"],
    )
    assert [(r["kind"], r["level"], r["rate"]) for r in rates] == [
        ("charge", "staff", 9500),
        ("charge", "staff", 15000),
        ("cost", "staff", 1850),
    ]
    assert str(rates[1]["effective_from"]) == "2026-04-01"
    assert str(rates[0]["effective_from"]) == "2026-02-01"  # default: the start date
    audit = (await audit_entries(org["id"], "staff.imported"))[0]
    assert audit["metadata"]["count"] == 3 and audit["metadata"]["rates"] == 3
    assert audit["actor_user_id"] == str(owner.id) and audit["target_type"] == "member"
    history = await scalar(
        "SELECT count(*) FROM rate_changes WHERE organization_id = :o AND source = 'import'",
        o=org["id"],
    )
    assert history == 3


async def test_generated_codes_skip_codes_in_the_file(client):
    owner, org = await practice(client)
    content = csv_of(
        "full_name,grade,start_date,code",
        "First,SNR,2026-01-01,",
        "Second,SNR,2026-01-01,S002",
        "Third,SNR,2026-01-01,",
    )
    data = (await upload(client, owner, org, "staff", content)).json()["data"]
    assert [i["code"] for i in data["items"]] == ["S003", "S002", "S004"]


BAD = csv_of(
    "full_name,grade,start_date,code,email,initials,leaving_date,hours_per_day,working_minutes_per_day,"
    "days,target,approver_code,cost_rate",
    "Good One,SNR,2026-01-05,DUP,,,,,,,,,",
    "Dup Code,SNR,2026-01-05,dup,,,,,,,,,",
    "Taken Code,SNR,2026-01-05,TAKEN,,,,,,,,,",
    "Bad Grade,NOPE,2026-01-05,,,,,,,,,,",
    "Bad Email,SNR,2026-01-05,,not-an-email,,,,,,,,",
    "No Start,SNR,,,,,,,,,,,",
    "Bad Date,SNR,31/02/2026,,,,,,,,,,",
    "Left Early,SNR,2026-03-01,,,,2026-01-01,,,,,,",
    "Both Hours,SNR,2026-03-01,,,,,7,420,,,,",
    "Bad Days,SNR,2026-03-01,,,,,,,Funday,,,",
    "Bad Target,SNR,2026-03-01,,,,,,,,150,,",
    "Ghost Approver,SNR,2026-03-01,,,,,,,,,NOBODY,",
    "File Approver,SNR,2026-03-01,,,,,,,,,DUP,",
    "Bad Rate,SNR,2026-03-01,,,,,,,,,,abc",
    ",SNR,2026-03-01,,,,,,,,,,",
)


async def test_dry_run_reports_every_row_error(client):
    owner, org = await practice(client)
    await new_staff(client, owner, org, "Existing", code="TAKEN")
    response = await upload(client, owner, org, "staff", BAD, dry_run=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    errors = {(e["row"], e["field"]): e for e in data["errors"]}
    assert errors.keys() == {
        (2, "code"),
        (3, "code"),
        (4, "grade"),
        (5, "email"),
        (6, "start_date"),
        (7, "start_date"),
        (8, "leaving_date"),
        (9, "hours_per_day"),
        (10, "working_days"),
        (11, "chargeable_target_percent"),
        (12, "approver_code"),
        (13, "approver_code"),
        (14, "cost_rate"),
        (15, "full_name"),
    }
    assert "also used on row 1" in errors[(2, "code")]["message"]
    assert "already has this code" in errors[(3, "code")]["message"]
    assert errors[(4, "grade")]["code"] == "unknown_grade"
    assert errors[(12, "approver_code")]["code"] == "unknown_staff"
    assert errors[(13, "approver_code")]["code"] == "approver_cannot_sign_in"
    assert data["total_rows"] == 15 and data["valid_rows"] == 1
    assert [i["name"] for i in data["items"]] == ["Good One"]


async def test_real_run_with_errors_writes_nothing(client):
    owner, org = await practice(client)
    before = await staff_count(org)
    seq_sql = (
        "SELECT next_number FROM document_sequences WHERE organization_id = :o AND kind = 'staff'"
    )
    seq_before = await scalar(seq_sql, o=org["id"])
    content = csv_of(
        "full_name,grade,start_date,cost_rate",
        "Fine,SNR,2026-01-05,20",
        "Broken,NOGRADE,2026-01-05,",
    )
    response = await upload(client, owner, org, "staff", content)
    assert response.status_code == 422
    body = error(response)
    assert body["code"] == "IMPORT_INVALID"
    assert [(d["row"], d["field"]) for d in body["details"]] == [(2, "grade")]
    assert await staff_count(org) == before
    assert (
        await scalar("SELECT count(*) FROM rate_rules WHERE organization_id = :o", o=org["id"]) == 0
    )
    assert await scalar(seq_sql, o=org["id"]) == seq_before
    assert await audit_entries(org["id"], "staff.imported") == []


async def test_database_errors_roll_back_only_their_row_and_the_whole_file(client):
    owner, org = await practice(client)
    content = csv_of(
        "full_name,grade,start_date,email,initials",
        "One,SNR,2026-01-05,same@firm.example,ZZ",
        "Two,SNR,2026-01-05,SAME@firm.example,",
        "Three,SNR,2026-01-05,,zz",
    )
    data = (await upload(client, owner, org, "staff", content, dry_run=True)).json()["data"]
    assert {(e["row"], e["field"]) for e in data["errors"]} == {(2, "email"), (3, "initials")}
    assert data["valid_rows"] == 1


async def test_existing_staff_member_with_a_login_can_approve(client):
    owner, org = await practice(client)
    manager = await staff(org, "manager", email="mgr@example.com")
    code = await scalar(
        "SELECT staff_code FROM organization_members WHERE organization_id = :o AND user_id = :u",
        o=org["id"],
        u=str(manager.id),
    )
    content = csv_of("full_name,grade,start_date,approver", f"Dee,SNR,2026-01-05,{code.lower()}")
    data = (await upload(client, owner, org, "staff", content)).json()["data"]
    assert data["created"] == 1
    approver = await scalar(
        "SELECT manager_user_id FROM organization_members WHERE organization_id = :o AND user_id = :u",
        o=org["id"],
        u=data["items"][0]["id"],
    )
    assert approver == manager.id


async def test_headers_aliases_unknown_columns_and_encodings(client):
    owner, org = await practice(client)
    content = "\ufeffName ,GRADE NAME,Joined,Colour\r\nZoë Zed,Junior,5/1/2026,Blue\r\n".encode()
    response = await upload(client, owner, org, "staff", content)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["created"] == 1
    assert data["ignored_columns"] == ["Colour"]
    assert data["columns"] == ["full_name", "grade", "start_date"]
    assert (
        await scalar("SELECT full_name FROM users WHERE id = :u", u=data["items"][0]["id"])
        == "Zoë Zed"
    )
    raw = await client.post(
        org_url(org, "/imports/staff"),
        headers={**owner.headers, "Content-Type": "text/csv"},
        content="full_name,grade,start_date\r\nCafé,JNR,2026-01-05\r\n".encode("cp1252"),
    )
    assert raw.status_code == 200, raw.text


@pytest.mark.parametrize(
    ("content", "status", "code"),
    [
        (csv_of("full_name,grade", "A,SNR"), 422, "IMPORT_MISSING_COLUMNS"),
        (
            csv_of("full_name,name,grade,start_date", "A,B,SNR,2026-01-05"),
            422,
            "IMPORT_DUPLICATE_COLUMNS",
        ),
        (b"", 400, "FILE_INVALID"),
        (csv_of("full_name,grade,start_date"), 400, "FILE_INVALID"),
    ],
)
async def test_file_level_errors(client, content, status, code):
    owner, org = await practice(client)
    response = await upload(client, owner, org, "staff", content)
    assert response.status_code == status, response.text
    assert error(response)["code"] == code


async def test_more_than_5000_rows_is_rejected(client):
    owner, org = await practice(client)
    content = csv_of("full_name,grade,start_date", *("P,SNR,2026-01-05" for _ in range(5001)))
    response = await upload(client, owner, org, "staff", content, dry_run=True)
    assert response.status_code == 422
    assert error(response)["code"] == "IMPORT_TOO_MANY_ROWS"


async def test_wrong_content_type_and_missing_file_field(client):
    owner, org = await practice(client)
    response = await client.post(
        org_url(org, "/imports/staff"), headers=owner.headers, json={"name": "A"}
    )
    assert response.status_code == 415
    response = await client.post(
        org_url(org, "/imports/staff"),
        headers=owner.headers,
        files={"upload": ("s.csv", b"full_name\r\nA\r\n", "text/csv")},
    )
    assert response.status_code == 400


async def test_import_permissions(client):
    _, org = await practice(client)
    _, other = await practice(client, "Other")
    content = csv_of("full_name,grade,start_date,cost_rate", "Pat,SNR,2026-01-05,20")
    # Practice permission alone, or the staff permission alone, is not enough.
    for actor in (
        await staff(org, "manager"),
        await staff(org, "staff"),
        await role_without(org, "data.import_export"),
        await role_with(org, "data.import_export"),
        await role_with(org, "member.invite"),
    ):
        assert (await upload(client, actor, org, "staff", content, dry_run=True)).status_code == 403
    # Without rate.manage the rate columns are row errors.
    inviter = await role_with(org, "data.import_export", "member.invite")
    data = (await upload(client, inviter, org, "staff", content, dry_run=True)).json()["data"]
    assert [(e["field"], e["code"]) for e in data["errors"]] == [("cost_rate", "permission_denied")]
    plain = csv_of("full_name,grade,start_date", "Pat,SNR,2026-01-05")
    assert (await upload(client, inviter, org, "staff", plain)).status_code == 200
    admin = await staff(org, "admin")
    assert (await upload(client, admin, org, "staff", content)).status_code == 200
    outsider = await staff(other, "owner")
    assert (await upload(client, outsider, org, "staff", content)).status_code == 404


async def test_export_columns_and_formula_guard(client):
    owner, org = await practice(client)
    await new_staff(
        client, owner, org, "=SUM(A1) Evil", code="EVIL", initials="EV", email="e@firm.example"
    )
    await new_staff(
        client,
        owner,
        org,
        "Wanda Worker",
        code="WW",
        working_days=[1, 3, 5],
        working_minutes_per_day=480,
        chargeable_target_bp=7250,
    )
    await upload(
        client,
        owner,
        org,
        "staff",
        csv_of("full_name,grade,start_date,cost_rate,charge_rate", "Rated,SNR,2026-01-05,12.5,80"),
    )
    response, rows = await export_rows(client, owner, org, "staff")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "staff.csv" in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf") and b"\r\n" in response.content
    by_code = {r["code"]: r for r in rows}
    assert by_code["EVIL"]["full_name"] == "'=SUM(A1) Evil"
    assert by_code["EVIL"]["email"] == "e@firm.example"
    ww = by_code["WW"]
    assert ww["working_days"] == "Mon;Wed;Fri" and ww["working_minutes_per_day"] == "480"
    assert ww["chargeable_target_percent"] == "72.5" and ww["grade"] == "SNR"
    assert ww["status"] == "active" and ww["login_status"] == "none"
    rated = next(r for r in rows if r["full_name"] == "Rated")
    assert rated["cost_rate"] == "12.50" and rated["charge_rate"] == "80.00"
    assert rows[0]["code"] <= rows[-1]["code"]
    assert {"cost_rate", "charge_rate"} <= rows[0].keys()
    assert by_code["WW"]["working_days"] != ""


async def test_export_hides_rates_without_rate_read_and_checks_permissions(client):
    owner, org = await practice(client)
    await new_staff(client, owner, org, "Wanda Worker", code="WW")
    reader = await role_with(org, "data.import_export", "member.read")
    response, rows = await export_rows(client, reader, org, "staff")
    assert response.status_code == 200
    assert "cost_rate" not in rows[0] and "charge_rate" not in rows[0]
    for actor in (
        await staff(org, "manager"),
        await role_with(org, "data.import_export"),
        await role_with(org, "member.read"),
    ):
        assert (await export_rows(client, actor, org, "staff"))[0].status_code == 403


async def test_export_round_trips_through_the_import_in_another_practice(client):
    owner, org = await practice(client)
    await new_staff(
        client, owner, org, "Wanda Worker", code="WW", working_days=[2, 4], initials="WW"
    )
    await new_staff(client, owner, org, "Xavier", code="XX", email="x@firm.example")
    _, rows = await export_rows(client, owner, org, "staff")
    # The practice's owner has no start date, which an import requires.
    rows = [r for r in rows if r["start_date"]]
    assert len(rows) == 2
    lines = [",".join(rows[0].keys())] + [",".join(r.values()) for r in rows]
    owner2, org2 = await practice(client, "Second")
    response = await upload(client, owner2, org2, "staff", csv_of(*lines))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["created"] == 2
    ignored = response.json()["data"]["ignored_columns"]
    assert ignored == ["status", "login_status"]
    _, again = await export_rows(client, owner2, org2, "staff")
    copy = {r["code"]: r for r in again}
    for row in rows:
        mine = copy[row["code"]]
        assert {k: v for k, v in mine.items() if k != "approver_code"} == {
            k: v for k, v in row.items() if k != "approver_code"
        }

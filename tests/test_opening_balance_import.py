"""Opening-balance time import: resolving codes/emails, tagging, auto-approving a fresh week,
and never approving a week that already had other content on it."""

from datetime import date

from tests.clients_helpers import org_url, scalar, sql
from tests.domain_factories import make_client, make_job, make_time_entry, practice, staff


def csv_of(*lines: str) -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode()


async def upload(client, actor, org, content: bytes, *, dry_run: bool = False):
    query = "?dry_run=true" if dry_run else ""
    return await client.post(
        org_url(org, f"/imports/opening_balance_time{query}"),
        headers=actor.headers,
        files={"file": ("opening_balances.csv", content, "text/csv")},
    )


async def sheet_status(org: dict, user_id, week_start: date) -> str | None:
    return await scalar(
        "SELECT status FROM timesheets WHERE organization_id = :o AND user_id = :u "
        "AND week_start = :w",
        o=org["id"],
        u=str(user_id),
        w=week_start,
    )


async def test_dry_run_validates_without_writing(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd", code="ACME")
    csv = csv_of(
        "client,staff,date,hours,billable",
        f"ACME,{owner.email},2024-01-15,3.5,Y",
    )
    response = await upload(client, owner, org, csv, dry_run=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["dry_run"] is True
    assert data["errors"] == []
    assert data["total_rows"] == 1 and data["valid_rows"] == 1 and data["created"] == 0
    assert await scalar(
        "SELECT count(*) FROM time_entries WHERE organization_id = :o", o=org["id"]
    ) == 0
    assert acme.code == "ACME"


async def test_real_import_creates_and_tags_entries_and_approves_the_fresh_week(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd", code="ACME")
    csv = csv_of(
        "client,staff,date,hours",
        f"ACME,{owner.email},2024-01-15,3.5",
        f"ACME,{owner.email},2024-01-16,2",
    )
    response = await upload(client, owner, org, csv)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["created"] == 2
    assert [e["minutes"] for e in data["entries"]] == [210, 120]

    rows = await sql(
        "SELECT entry_date, minutes, is_opening_balance, billing_status, client_id "
        "FROM time_entries WHERE organization_id = :o ORDER BY entry_date",
        o=org["id"],
    )
    assert len(rows) == 2
    assert all(r["is_opening_balance"] for r in rows)
    assert all(r["billing_status"] == "unbilled" for r in rows)
    assert all(str(r["client_id"]) == str(acme.id) for r in rows)

    # The week (Monday 2024-01-15 is a Monday) was created only by this import, so it is
    # approved automatically: the time is immediately usable as WIP.
    assert await sheet_status(org, owner.id, date(2024, 1, 15)) == "approved"


async def test_resolves_activity_and_job_by_code(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd", code="ACME")
    job = await make_job(org, acme, "Audit", number="TJ00001")
    csv = csv_of(
        "client,staff,date,hours,activity,job",
        f"ACME,{owner.email},2024-02-01,1,ACC,{job.number}",
    )
    response = await upload(client, owner, org, csv)
    assert response.status_code == 200, response.text
    rows = await sql(
        "SELECT job_id, activity_code_id FROM time_entries WHERE organization_id = :o",
        o=org["id"],
    )
    assert str(rows[0]["job_id"]) == str(job.id)
    assert rows[0]["activity_code_id"] is not None


async def test_unknown_client_and_staff_are_row_errors_and_nothing_is_written(client):
    owner, org = await practice(client)
    csv = csv_of(
        "client,staff,date,hours",
        f"NOPE,{owner.email},2024-01-15,1",
        "ACME,nobody@example.com,2024-01-15,1",
    )
    response = await upload(client, owner, org, csv)
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "IMPORT_INVALID"
    fields = {d["field"] for d in body["details"]}
    assert fields == {"client", "staff"}
    assert await scalar(
        "SELECT count(*) FROM time_entries WHERE organization_id = :o", o=org["id"]
    ) == 0


async def test_job_must_belong_to_the_row_s_client(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd", code="ACME")
    other = await make_client(org, "Other Ltd", code="OTHER")
    job = await make_job(org, other, "Audit", number="TJ00002")
    csv = csv_of(
        "client,staff,date,hours,job",
        f"ACME,{owner.email},2024-01-15,1,{job.number}",
    )
    response = await upload(client, owner, org, csv)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"][0]["code"] == "wrong_client"
    assert acme.code == "ACME"


async def test_invalid_date_and_hours_are_row_errors(client):
    owner, org = await practice(client)
    await make_client(org, "Acme Ltd", code="ACME")
    csv = csv_of(
        "client,staff,date,hours",
        f"ACME,{owner.email},15/01/2024,1",  # not ISO
        f"ACME,{owner.email},2024-01-16,30",  # more than 24 hours
    )
    response = await upload(client, owner, org, csv)
    assert response.status_code == 422, response.text
    fields = {d["field"] for d in response.json()["error"]["details"]}
    assert fields == {"date", "hours"}


async def test_never_approves_a_week_that_already_had_other_content(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd", code="ACME")
    # A live, unrelated, unsubmitted entry already exists in the same week (not from this
    # import), on a sheet that is still "open".
    await make_time_entry(
        org, owner, client=acme, entry_date=date(2024, 1, 16), timesheet_status="open"
    )
    assert await sheet_status(org, owner.id, date(2024, 1, 15)) == "open"

    csv = csv_of("client,staff,date,hours", f"ACME,{owner.email},2024-01-17,1")
    response = await upload(client, owner, org, csv)
    assert response.status_code == 200, response.text
    # The week already had other, unrelated content, so the import must not approve it: that
    # would also approve the live entry that has nothing to do with this import.
    assert await sheet_status(org, owner.id, date(2024, 1, 15)) == "open"
    rows = await sql(
        "SELECT entry_date, is_opening_balance FROM time_entries WHERE organization_id = :o "
        "ORDER BY entry_date",
        o=org["id"],
    )
    assert [(r["entry_date"], r["is_opening_balance"]) for r in rows] == [
        (date(2024, 1, 16), False),
        (date(2024, 1, 17), True),
    ]


async def test_needs_opening_balance_import_permission(client):
    _owner, org = await practice(client)
    member = await staff(org, "staff", email="helper@example.com")
    await make_client(org, "Acme Ltd", code="ACME")
    csv = csv_of("client,staff,date,hours", f"ACME,{member.email},2024-01-15,1")
    response = await upload(client, member, org, csv)
    assert response.status_code == 403, response.text

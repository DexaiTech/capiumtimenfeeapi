"""APT64: the built-in Internal client internal time goes on. Every practice has one, with a
non-billable job per non-chargeable activity code; it is offered when time is recorded and left
out of everything about clients, WIP, billing, reports and the central client service."""

from datetime import date

from httpx import AsyncClient

from app.db.models import Client, Job
from app.db.session import get_sessionmaker
from app.services.practice_defaults import ACTIVITY_CODES, internal_job_number
from tests.billing_helpers import draft, set_org
from tests.central_helpers import BAKERY, link, new_team
from tests.clients_helpers import create_client, org_url, sql
from tests.domain_factories import (
    activity_code_id,
    make_client,
    make_job,
    make_time_entry,
    practice,
    staff,
)
from tests.factories import error
from tests.reports_helpers import report
from tests.time_helpers import log_time

INTERNAL_JOBS = {
    internal_job_number(code): name for code, name, chargeable in ACTIVITY_CODES if not chargeable
}


async def internal_client(org: dict) -> dict:
    [row] = await sql(
        "SELECT id, code, name, status, branch_id FROM clients "
        "WHERE organization_id = :o AND is_internal",
        o=org["id"],
    )
    return row


async def internal_jobs(org: dict) -> dict[str, dict]:
    rows = await sql(
        "SELECT j.id, j.number, j.name, j.billing_basis, j.status FROM jobs j "
        "JOIN clients c ON c.id = j.client_id WHERE j.organization_id = :o AND c.is_internal",
        o=org["id"],
    )
    return {row["number"]: row for row in rows}


async def test_a_new_practice_gets_one_internal_client_with_a_job_per_internal_activity(client):
    _, org = await practice(client)
    internal = await internal_client(org)
    assert (internal["code"], internal["name"], internal["status"]) == (
        "INTERNAL",
        "Internal",
        "active",
    )
    jobs = await internal_jobs(org)
    assert {number: job["name"] for number, job in jobs.items()} == INTERNAL_JOBS
    assert set(INTERNAL_JOBS) == {"INT-ADM", "INT-TRN", "INT-BDV", "INT-HOL"}
    assert {(job["billing_basis"], job["status"]) for job in jobs.values()} == {
        ("non_billable", "in_progress")
    }
    # Another practice has its own.
    _, other = await practice(client, "Other Practice")
    assert (await internal_client(other))["id"] != internal["id"]


async def test_the_client_list_counts_and_export_leave_it_out(client):
    owner, org = await practice(client)
    internal = await internal_client(org)
    acme = await create_client(client, owner, org, "Acme Ltd")

    listed = await client.get(org_url(org, "/clients"), headers=owner.headers)
    assert [c["id"] for c in listed.json()["data"]] == [acme["id"]]
    searched = await client.get(
        org_url(org, "/clients"), headers=owner.headers, params={"q": "internal"}
    )
    assert searched.json()["data"] == []
    without_branch = await client.get(
        org_url(org, "/clients"), headers=owner.headers, params={"without_branch": True}
    )
    assert [c["id"] for c in without_branch.json()["data"]] == [acme["id"]]

    summary = (await client.get(org_url(org, "/clients/summary"), headers=owner.headers)).json()
    assert summary["data"]["total"] == 1
    assert summary["data"]["by_status"]["active"] == 1

    export = await client.get(org_url(org, "/clients/export"), headers=owner.headers)
    assert export.status_code == 200
    assert "INTERNAL" not in export.text and "Acme Ltd" in export.text

    # The time pickers ask for it.
    for_time = await client.get(
        org_url(org, "/clients"), headers=owner.headers, params={"include_internal": True}
    )
    rows = {c["id"]: c for c in for_time.json()["data"]}
    assert set(rows) == {acme["id"], str(internal["id"])}
    assert rows[str(internal["id"])]["is_internal"] is True
    assert rows[acme["id"]]["is_internal"] is False
    one = await client.get(org_url(org, f"/clients/{internal['id']}"), headers=owner.headers)
    assert one.status_code == 200 and one.json()["data"]["is_internal"] is True


async def test_its_jobs_are_listed_only_for_it(client):
    owner, org = await practice(client)
    internal = await internal_client(org)
    acme = await make_client(org, "Acme Ltd")
    job = await make_job(org, acme, "Accounts")

    listed = await client.get(org_url(org, "/jobs"), headers=owner.headers)
    assert [j["id"] for j in listed.json()["data"]] == [str(job.id)]
    own = await client.get(
        org_url(org, "/jobs"), headers=owner.headers, params={"client_id": str(internal["id"])}
    )
    assert {j["number"] for j in own.json()["data"]} == set(INTERNAL_JOBS)


async def test_time_on_it_is_never_billable(client):
    _, org = await practice(client)
    person = await staff(org)
    jobs = await internal_jobs(org)
    training = jobs["INT-TRN"]["id"]
    chargeable = await activity_code_id(org, "ACC")

    entry = await log_time(client, person, org, job_id=training, activity_code_id=chargeable)
    assert entry["is_billable"] is False
    assert entry["client_name"] == "Internal"

    refused = await log_time(client, person, org, job_id=training, is_billable=True, status=422)
    assert refused["details"][0]["field"] == "is_billable"

    # Nor once it is edited onto it.
    acme = await make_client(org, "Acme Ltd")
    billable = await log_time(client, person, org, client_id=acme.id)
    assert billable["is_billable"] is True
    moved = await client.patch(
        org_url(org, f"/time-entries/{billable['id']}"),
        headers=person.headers,
        json={"job_id": str(training)},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["data"]["is_billable"] is False


async def test_it_cannot_be_edited_archived_deleted_or_given_engagements(client):
    owner, org = await practice(client)
    internal_id = (await internal_client(org))["id"]
    url = org_url(org, f"/clients/{internal_id}")

    for response in (
        await client.patch(url, headers=owner.headers, json={"name": "Our clients"}),
        await client.patch(url, headers=owner.headers, json={"status": "archived"}),
        await client.post(f"{url}/archive", headers=owner.headers),
        await client.delete(url, headers=owner.headers),
        await client.post(
            org_url(org, "/clients/bulk"),
            headers=owner.headers,
            json={"action": "set_status", "client_ids": [str(internal_id)], "value": "archived"},
        ),
        await client.post(
            org_url(org, "/clients/bulk"),
            headers=owner.headers,
            json={"action": "delete", "client_ids": [str(internal_id)]},
        ),
    ):
        assert response.status_code == 409, response.text
        assert error(response)["code"] == "CLIENT_INTERNAL"

    services = await sql(
        "SELECT id FROM services WHERE organization_id = :o AND code = 'BOOKS'", o=org["id"]
    )
    engagement = await client.post(
        f"{url}/services", headers=owner.headers, json={"service_id": str(services[0]["id"])}
    )
    assert engagement.status_code == 409 and error(engagement)["code"] == "CLIENT_INTERNAL"

    after = await internal_client(org)
    assert (after["name"], after["status"]) == ("Internal", "active")


async def test_it_is_never_billed_and_has_no_wip(client):
    owner, org = await practice(client)
    internal = await internal_client(org)
    internal_id = internal["id"]
    jobs = await internal_jobs(org)

    refused = await draft(client, owner, org, internal_id, expect=409)
    assert refused["code"] == "CLIENT_INTERNAL"
    for path in (f"/clients/{internal_id}/wip", f"/clients/{internal_id}/billing-summary"):
        response = await client.get(org_url(org, path), headers=owner.headers)
        assert response.status_code == 409, path
        assert error(response)["code"] == "CLIENT_INTERNAL"

    # Even billable time written there directly is not WIP, nor in job profitability.
    async with get_sessionmaker()() as session:
        holder = await session.get(Client, internal_id)
        job = await session.get(Job, jobs["INT-ADM"]["id"])
    await make_time_entry(
        org, owner, client=holder, job=job, entry_date=date(2026, 9, 7), is_billable=True
    )
    wip = await client.get(org_url(org, "/wip"), headers=owner.headers)
    assert wip.status_code == 200 and wip.json()["data"] == []
    profitability = await report(
        client,
        owner,
        org,
        "job-profitability",
        date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 30),
    )
    assert profitability["rows"] == []


async def test_branches_on_do_not_apply_to_it(client):
    owner, org = await practice(client)
    await set_org(org, branches_enabled=True)
    person = await staff(org)
    internal = await internal_client(org)
    assert internal["branch_id"] is None
    entry = await log_time(client, person, org)
    assert entry["client_id"] == str(internal["id"])
    branch = await client.post(
        org_url(org, "/branches"), headers=owner.headers, json={"name": "London"}
    )
    assert branch.status_code == 201, branch.text
    response = await client.post(
        org_url(org, "/clients/bulk"),
        headers=owner.headers,
        json={
            "action": "set_branch",
            "client_ids": [str(internal["id"])],
            "value": branch.json()["data"]["id"],
        },
    )
    assert response.status_code == 409 and error(response)["code"] == "CLIENT_INTERNAL"


async def test_the_central_client_service_never_links_it(client: AsyncClient):
    team = await new_team(client)
    internal_id = (await internal_client(team.org))["id"]
    response = await link(client, team.owner, team.org, internal_id, BAKERY)
    assert response.status_code == 409 and error(response)["code"] == "CLIENT_INTERNAL"
    rows = await sql("SELECT central_client_id FROM clients WHERE id = :i", i=internal_id)
    assert rows == [{"central_client_id": None}]

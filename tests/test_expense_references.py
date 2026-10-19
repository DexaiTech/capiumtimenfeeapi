"""Expense lines count as use of their client and their job: neither can be deleted while a claim's
line refers to it (the same rule as time, disbursements and invoices), and can be once the lines
are gone."""

import uuid

from tests.clients_helpers import scalar
from tests.domain_factories import make_client, make_job
from tests.expense_helpers import (
    add_line,
    claim_body,
    claim_url,
    create_claim,
    expense_team,
    line_body,
)
from tests.factories import error
from tests.time_helpers import err


async def test_a_client_with_expense_lines_cannot_be_deleted(client):
    team = await expense_team(client)
    org, owner = team.org, team.owner
    acme = await make_client(org, "Acme Ltd")
    claim = await create_claim(client, owner, org)
    await add_line(client, owner, org, claim["id"], client_id=str(acme.id))
    url = team.url(f"/clients/{acme.id}")
    refused = await client.delete(url, headers=owner.headers)
    failure = err(refused, 409)
    assert failure["code"] == "CLIENT_IN_USE" and "expenses" in failure["message"]
    assert failure["details"] == {
        "jobs": 0,
        "time_entries": 0,
        "disbursements": 0,
        "expenses": 1,
        "invoices": 0,
        "credit_notes": 0,
        "payments": 0,
    }
    assert await scalar("SELECT deleted_at FROM clients WHERE id = :i", i=str(acme.id)) is None
    # Once the claim is gone the client can go.
    assert (
        await client.delete(claim_url(org, claim["id"]), headers=owner.headers)
    ).status_code == 200
    assert (await client.delete(url, headers=owner.headers)).status_code == 200


async def test_a_job_with_expense_lines_cannot_be_deleted(client):
    team = await expense_team(client)
    org, owner = team.org, team.owner
    acme = await make_client(org, "Acme Ltd")
    job = await make_job(org, acme, "Audit")
    claim = await create_claim(client, owner, org)
    line = await add_line(
        client, owner, org, claim["id"], client_id=str(acme.id), job_id=str(job.id)
    )
    url = team.url(f"/jobs/{job.id}")
    refused = await client.delete(url, headers=owner.headers)
    assert refused.status_code == 409 and error(refused)["code"] == "JOB_IN_USE"
    # The line only moves to another job of the client, or goes, and then the job can be deleted.
    removed = await client.delete(
        claim_url(org, claim["id"], f"/lines/{line['id']}"), headers=owner.headers
    )
    assert removed.status_code == 200
    assert (await client.delete(url, headers=owner.headers)).status_code == 200


async def test_a_reference_that_does_not_exist_answers_404_naming_the_field(client):
    team = await expense_team(client)
    org, owner = team.org, team.owner
    stranger = await expense_team(client, "Other Practice")
    foreign_client = await make_client(stranger.org, "Their Ltd")
    claim = await create_claim(client, owner, org)
    for field, value in (
        ("client_id", str(uuid.uuid4())),
        ("client_id", str(foreign_client.id)),  # another practice's client
        ("job_id", str(uuid.uuid4())),
        ("expense_type_id", str(uuid.uuid4())),
    ):
        body = {**await line_body(org), field: value}
        url = claim_url(org, claim["id"], "/lines")
        failure = err(await client.post(url, headers=owner.headers, json=body), 404)
        assert failure["code"] == "RESOURCE_NOT_FOUND" and failure["details"] == {"field": field}
    # The same on a whole claim: one unknown reference fails it, and nothing is written.
    body = await claim_body(org, lines=1, user_id=str(uuid.uuid4()))
    refused = await client.post(claim_url(org), headers=owner.headers, json=body)
    assert err(refused, 404)["details"] == {"field": "user_id"}

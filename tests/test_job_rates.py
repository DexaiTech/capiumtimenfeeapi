"""The rates of a client's jobs on the client's Jobs page (APT54): each job follows the task
(service) rate until the accountant agrees a custom client rate for it, which is the job's own
hourly rate and is what its time is then billed at."""

import uuid

from httpx import AsyncClient

from tests.domain_factories import make_client, make_job, practice, staff
from tests.team_helpers import grade_id, new_rate, set_grade
from tests.time_helpers import err, log_time, ok_data, org_url, set_practice


async def _service(client: AsyncClient, owner, org: dict, code: str = "ANN") -> dict:
    response = await client.post(
        org_url(org, "/services"),
        headers=owner.headers,
        json={"code": code, "name": f"Annual accounts {code}"},
    )
    return ok_data(response, 201)


async def _rates(client: AsyncClient, actor, org: dict, client_id: uuid.UUID) -> dict:
    response = await client.get(
        org_url(org, f"/clients/{client_id}/job-rates"), headers=actor.headers
    )
    return ok_data(response)


def _job(data: dict, job_id: uuid.UUID) -> dict:
    return next(row for row in data["jobs"] if row["job_id"] == str(job_id))


async def test_a_job_follows_the_task_rate_until_a_client_rate_is_agreed(client):
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    await set_grade(org, owner, "SNR")
    # The practice usually charges £100 an hour for the task (seniors), £150 (partners).
    for code, rate in (("SNR", 10_000), ("PTR", 15_000)):
        await new_rate(
            client,
            owner,
            org,
            kind="charge",
            service_id=service["id"],
            grade_id=await grade_id(org, code),
            rate=rate,
            effective_from="2026-01-01",
        )
    anker = await make_client(org, "Anker Technologies")
    accounts = await make_job(org, anker, "Annual Accounts", service_id=uuid.UUID(service["id"]))
    fixed = await make_job(org, anker, "Payroll", billing_basis="fixed", fixed_fee=150_000)
    plain = await make_job(org, anker, "Advice")
    off = await make_job(org, anker, "Pro bono", billing_basis="non_billable")

    data = await _rates(client, owner, org, anker.id)
    assert data["show_rates"] is True and len(data["jobs"]) == 4
    row = _job(data, accounts.id)
    assert row["source"] == "task" and row["job_rate"] is None
    assert row["task_rate"] == {"low": 10_000, "high": 15_000, "rules": 2}
    assert row["client_rule"] is None and row["person_rules"] == 0
    assert _job(data, fixed.id)["source"] == "fixed_fee"
    assert _job(data, fixed.id)["fixed_fee"] == 150_000
    assert _job(data, plain.id)["source"] == "staff" and _job(data, plain.id)["task_rate"] is None
    assert _job(data, off.id)["source"] == "non_billable"

    before = await log_time(client, owner, org, job_id=accounts.id)
    assert before["charge_rate"] == 10_000

    # The accountant agrees £120 an hour with the client for this job.
    response = await client.patch(
        org_url(org, f"/jobs/{accounts.id}"), headers=owner.headers, json={"hourly_rate": 12_000}
    )
    assert ok_data(response)["hourly_rate"] == 12_000
    row = _job(await _rates(client, owner, org, anker.id), accounts.id)
    assert row["source"] == "job" and row["job_rate"] == 12_000
    # Billing uses it from now on; time already recorded keeps its rate until it is re-rated.
    assert (await log_time(client, owner, org, job_id=accounts.id))["charge_rate"] == 12_000
    kept = await client.get(org_url(org, f"/time-entries/{before['id']}"), headers=owner.headers)
    assert ok_data(kept)["charge_rate"] == 10_000

    # Back to the task rate.
    response = await client.patch(
        org_url(org, f"/jobs/{accounts.id}"), headers=owner.headers, json={"hourly_rate": None}
    )
    assert ok_data(response)["hourly_rate"] is None
    assert _job(await _rates(client, owner, org, anker.id), accounts.id)["source"] == "task"


async def test_a_rate_agreed_with_the_client_for_everyone_comes_first(client):
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    anker = await make_client(org, "Anker Technologies")
    accounts = await make_job(
        org, anker, "Annual Accounts", service_id=uuid.UUID(service["id"]), hourly_rate=12_000
    )
    plain = await make_job(org, anker, "Advice", hourly_rate=9_000)
    rule = await new_rate(
        client,
        owner,
        org,
        kind="charge",
        client_id=anker.id,
        service_id=service["id"],
        rate=11_000,
        effective_from="2026-01-01",
    )
    person = await staff(org, "manager")
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        client_id=anker.id,
        user_id=person.id,
        rate=13_000,
        effective_from="2026-01-01",
    )
    data = await _rates(client, owner, org, anker.id)
    row = _job(data, accounts.id)
    assert row["source"] == "client_rule" and row["job_rate"] == 12_000
    assert row["client_rule"]["id"] == rule["id"]
    assert row["client_rule"]["level"] == "client_service"
    assert row["client_rule"]["rate"] == 11_000
    assert row["person_rules"] == 1
    # The client's rate for the service does not reach a job of another service.
    assert _job(data, plain.id)["source"] == "job"


async def test_rule_rates_need_rate_read_and_the_client_must_exist(client):
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        service_id=service["id"],
        grade_id=await grade_id(org),
        rate=10_000,
        effective_from="2026-01-01",
    )
    anker = await make_client(org)
    job = await make_job(org, anker, service_id=uuid.UUID(service["id"]), hourly_rate=12_000)
    member = await staff(org, "staff")
    data = await _rates(client, member, org, anker.id)
    row = _job(data, job.id)
    assert data["show_rates"] is False
    assert row["job_rate"] == 12_000 and row["source"] == "job"
    assert row["task_rate"] is None and row["person_rules"] is None

    missing = await client.get(
        org_url(org, f"/clients/{uuid.uuid4()}/job-rates"), headers=owner.headers
    )
    assert err(missing, 404)["code"] == "RESOURCE_NOT_FOUND"


async def test_staff_rates_only_leaves_out_the_grade_rates_for_the_service(client):
    """APT79: in the staff rates only mode a grade's rate for the service never prices time, so
    the Jobs page does not offer it as the task rate; a person's own rate for the service is."""
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    await set_grade(org, owner, "SNR")
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        service_id=service["id"],
        grade_id=await grade_id(org, "SNR"),
        rate=10_000,
        effective_from="2026-01-01",
    )
    anker = await make_client(org, "Anker Technologies")
    accounts = await make_job(org, anker, "Annual Accounts", service_id=uuid.UUID(service["id"]))
    assert _job(await _rates(client, owner, org, anker.id), accounts.id)["source"] == "task"

    await set_practice(org, rate_mode="staff_only")
    row = _job(await _rates(client, owner, org, anker.id), accounts.id)
    assert row["source"] == "staff" and row["task_rate"] is None

    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        service_id=service["id"],
        user_id=owner.id,
        rate=11_000,
        effective_from="2026-01-01",
    )
    row = _job(await _rates(client, owner, org, anker.id), accounts.id)
    assert row["source"] == "task"
    assert row["task_rate"] == {"low": 11_000, "high": 11_000, "rules": 1}
    # It is what the time is billed at.
    assert (await log_time(client, owner, org, job_id=accounts.id))["charge_rate"] == 11_000

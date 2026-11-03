"""The "service" rate level (APT68): one charge-out rate for a service, whoever does the work.

It is the last source of step 5 of the charge-out ladder: a person's own rate for the service and
their grade's rate for it win over it; it wins over the person's own and their grade's rates. Any
client rate or the job's own rate still wins. The cost rate never depends on the service."""

from httpx import AsyncClient

from tests.clients_helpers import sql
from tests.domain_factories import make_client, make_job, practice, service_id, staff
from tests.team_helpers import grade_id, new_rate
from tests.test_rates_v2 import FROM, resolved, world
from tests.time_helpers import entry_row, log_time, ok_data, org_url, set_practice


async def test_a_rate_for_a_service_alone_is_a_level_of_its_own(client: AsyncClient):
    owner, org, _person, _acme, svc, _job = await world(client)
    created = await new_rate(
        client, owner, org, kind="charge", service_id=svc, rate=5_000, effective_from=FROM
    )
    assert created["level"] == "service"
    assert created["level_label"] == "Service (everyone)"
    assert created["precedence"] == 5
    listed = ok_data(
        await client.get(
            org_url(org, "/rates"),
            headers=owner.headers,
            params={"kind": "charge", "level": "service", "service_id": str(svc)},
        )
    )
    assert [rule["id"] for rule in listed] == [created["id"]]


async def test_it_comes_after_the_rates_for_the_person_and_the_grade_on_the_service(
    client: AsyncClient,
):
    owner, org, person, _acme, svc, job = await world(client)
    snr = await grade_id(org, "SNR")
    await sql("UPDATE jobs SET hourly_rate = NULL WHERE id = :j", j=str(job.id))
    await new_rate(
        client, owner, org, kind="charge", user_id=person.id, rate=4_600, effective_from=FROM
    )
    await new_rate(client, owner, org, kind="charge", grade_id=snr, rate=4_000, effective_from=FROM)
    before = await resolved(client, owner, org, person, job_id=job.id)
    assert before["charge_source"] == "staff"

    await new_rate(
        client, owner, org, kind="charge", service_id=svc, rate=5_000, effective_from=FROM
    )
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (5_000, "service")
    steps = found["charge_ladder"]
    assert [i for i, step in enumerate(steps) if step["selected"]] == [4]
    assert steps[4]["label"] == "Service rate"

    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        service_id=svc,
        grade_id=snr,
        rate=5_500,
        effective_from=FROM,
    )
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (5_500, "service_grade")

    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        service_id=svc,
        user_id=person.id,
        rate=5_900,
        effective_from=FROM,
    )
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (5_900, "service_staff")


async def test_a_client_rate_and_the_job_rate_still_win(client: AsyncClient):
    owner, org, person, acme, svc, job = await world(client)
    await new_rate(
        client, owner, org, kind="charge", service_id=svc, rate=5_000, effective_from=FROM
    )
    # The job has a rate of 6 400 in this world: it is step 4, above the new level.
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (6_400, "job")
    await new_rate(
        client, owner, org, kind="charge", client_id=acme.id, rate=7_000, effective_from=FROM
    )
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert found["charge_source"] == "client"


async def test_each_service_has_its_own_rate_and_the_cost_stays_the_persons(
    client: AsyncClient,
):
    owner, org = await practice(client)
    await set_practice(org, default_charge_rate=2_000, time_increment_minutes=1)
    accounts = await service_id(org, "ACCS")
    vat = await service_id(org, "VAT")
    person = await staff(org)  # no grade: the service's rate still applies
    await new_rate(
        client, owner, org, kind="cost", user_id=person.id, rate=1_500, effective_from=FROM
    )
    await new_rate(
        client, owner, org, kind="charge", service_id=accounts, rate=6_000, effective_from=FROM
    )
    await new_rate(
        client, owner, org, kind="charge", service_id=vat, rate=8_000, effective_from=FROM
    )
    acme = await make_client(org)
    priced = []
    for svc in (accounts, vat, None):
        job = await make_job(org, acme, service_id=svc)
        entry = await log_time(client, person, org, job_id=job.id, minutes=60)
        row = await entry_row(entry["id"])
        priced.append((row.charge_rate, row.rate_source, row.cost_rate))
    assert priced == [
        (6_000, "service", 1_500),
        (8_000, "service", 1_500),
        (2_000, "default", 1_500),
    ]

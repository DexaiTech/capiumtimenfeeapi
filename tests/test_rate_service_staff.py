"""The "staff + service" rate level: a person's own rate for one service, for every client.

It shares step 5 of the charge-out ladder with ``service_grade`` and wins over it; any client
rate or the job's own rate still wins over both."""

from httpx import AsyncClient

from tests.clients_helpers import sql
from tests.domain_factories import make_client, make_job, practice, service_id, staff
from tests.team_helpers import grade_id, new_rate, set_grade
from tests.test_rates_v2 import DAY, FROM, resolved, world
from tests.time_helpers import entry_row, log_time, org_url, set_practice


async def test_a_rate_for_a_person_and_a_service_is_a_level_of_its_own(client: AsyncClient):
    owner, org, person, _acme, svc, _job = await world(client)
    created = await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        service_id=svc,
        rate=5_200,
        effective_from=FROM,
    )
    assert created["level"] == "service_staff"
    assert created["level_label"] == "Staff + service"
    assert created["precedence"] == 5


async def test_cost_rates_cannot_be_set_for_a_service(client: AsyncClient):
    owner, org, person, _acme, svc, _job = await world(client)
    response = await client.post(
        org_url(org, "/rates"),
        headers=owner.headers,
        json={
            "kind": "cost",
            "user_id": str(person.id),
            "service_id": str(svc),
            "rate": 1_000,
            "effective_from": FROM,
        },
    )
    assert response.status_code == 422, response.text
    assert "Cost rates exist only for a staff member or a grade" in response.text


async def test_it_beats_the_grade_rate_for_the_service_and_the_staff_rate(client: AsyncClient):
    owner, org, person, _acme, svc, job = await world(client)
    snr = await grade_id(org, "SNR")
    await sql("UPDATE jobs SET hourly_rate = NULL WHERE id = :j", j=str(job.id))
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
    await new_rate(
        client, owner, org, kind="charge", user_id=person.id, rate=4_600, effective_from=FROM
    )
    before = await resolved(client, owner, org, person, job_id=job.id)
    assert (before["charge_rate"], before["charge_source"]) == (5_500, "service_grade")

    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        service_id=svc,
        rate=5_900,
        effective_from=FROM,
    )
    after = await resolved(client, owner, org, person, job_id=job.id)
    assert (after["charge_rate"], after["charge_source"]) == (5_900, "service_staff")
    steps = after["charge_ladder"]
    assert [i for i, step in enumerate(steps) if step["selected"]] == [4]
    assert steps[4]["label"] == "Staff rate for the service"


async def test_a_client_rate_and_the_job_rate_still_win(client: AsyncClient):
    owner, org, person, acme, svc, job = await world(client)
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        service_id=svc,
        rate=5_900,
        effective_from=FROM,
    )
    # The job has a rate of 6 400 in this world: it is step 4, above the new level.
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (6_400, "job")
    await new_rate(
        client, owner, org, kind="charge", client_id=acme.id, rate=7_000, effective_from=FROM
    )
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert found["charge_source"] == "client"


async def test_it_only_applies_to_that_person_and_that_service(client: AsyncClient):
    owner, org, person, acme, svc, _job = await world(client)
    other_person = await staff(org)
    await set_grade(org, other_person, "SNR")
    vat = await service_id(org, "VAT")
    await set_practice(org, default_charge_rate=2_000)
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        service_id=svc,
        rate=5_900,
        effective_from=FROM,
    )
    on_service = await make_job(org, acme, service_id=svc)
    on_other = await make_job(org, acme, service_id=vat)
    assert (await resolved(client, owner, org, person, job_id=on_service.id))[
        "charge_source"
    ] == "service_staff"
    assert (await resolved(client, owner, org, person, job_id=on_other.id))[
        "charge_source"
    ] != "service_staff"
    assert (await resolved(client, owner, org, other_person, job_id=on_service.id))[
        "charge_source"
    ] != "service_staff"


async def test_time_is_priced_by_it_for_any_client_and_records_the_source(client: AsyncClient):
    owner, org = await practice(client)
    await set_practice(org, default_charge_rate=2_000, time_increment_minutes=1)
    svc = await service_id(org, "ACCS")
    person = await staff(org)
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        service_id=svc,
        rate=6_000,
        effective_from="2026-01-01",
    )
    for _ in range(2):
        acme = await make_client(org)
        job = await make_job(org, acme, service_id=svc)
        entry = await log_time(client, person, org, job_id=job.id, minutes=60)
        assert (entry["charge_rate"], entry["rate_source"]) == (6_000, "service_staff"), entry
        assert (await entry_row(entry["id"])).rate_source == "service_staff"


async def test_two_rates_of_one_person_and_service_cannot_overlap(client: AsyncClient):
    owner, org, person, _acme, svc, _job = await world(client)
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        service_id=svc,
        rate=5_900,
        effective_from=FROM,
    )
    response = await client.post(
        org_url(org, "/rates"),
        headers=owner.headers,
        json={
            "kind": "charge",
            "user_id": str(person.id),
            "service_id": str(svc),
            "rate": 6_100,
            "effective_from": DAY,
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RATE_OVERLAP"

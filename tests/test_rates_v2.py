"""Dated rate rules (/rates): the precedence ladder, periods and overlaps, used rates, history,
rate preview, permissions and tenant isolation. Re-rating is in ``test_rerate.py``."""

import asyncio
import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

from tests.clients_helpers import count_queries, sql
from tests.domain_factories import (
    make_client,
    make_job,
    make_staff_rate,
    practice,
    service_id,
    staff,
)
from tests.factories import add_member, audit_entries
from tests.foundation_helpers import add_custom_role
from tests.team_helpers import (
    grade_id,
    new_rate,
    rate_error,
    rule_count,
    set_grade,
    set_grade_rates,
)
from tests.time_helpers import entry_row, log_time, ok_data, org_url, set_practice

DAY = "2026-09-07"
FROM = "2026-01-01"


async def resolved(client: AsyncClient, actor, org: dict, person, **params) -> dict:
    query = {"user_id": str(person.id), "date": DAY} | {k: str(v) for k, v in params.items()}
    response = await client.get(org_url(org, "/rates/resolve"), headers=actor.headers, params=query)
    return ok_data(response)


async def world(client: AsyncClient):
    """A practice with a graded staff member, a client, a service and a job that has all four
    of them, and a practice default rate: the setting for the ladder tests."""
    owner, org = await practice(client)
    await set_practice(org, default_charge_rate=2_000)
    person = await staff(org)
    await set_grade(org, person, "SNR")
    acme = await make_client(org)
    svc = await service_id(org, "ACCS")
    job = await make_job(org, acme, service_id=svc, hourly_rate=6_400)
    return owner, org, person, acme, svc, job


# --- Precedence ladder -------------------------------------------------------------------


async def test_precedence_ladder_exact_pence_at_every_level(client: AsyncClient) -> None:
    owner, org, person, acme, svc, job = await world(client)
    snr = await grade_id(org, "SNR")
    await set_grade_rates(org, "SNR", charge=3_000, cost=700)
    ladder = [
        (
            "client_service_staff",
            {"client_id": acme.id, "service_id": svc, "user_id": person.id},
            9_100,
        ),
        ("client_service", {"client_id": acme.id, "service_id": svc}, 8_200),
        ("client_staff", {"client_id": acme.id, "user_id": person.id}, 7_300),
        ("client", {"client_id": acme.id}, 7_000),
        ("service_grade", {"service_id": svc, "grade_id": snr}, 5_500),
        ("staff", {"user_id": person.id}, 4_600),
        ("grade", {"grade_id": snr}, 3_700),
    ]
    rule_ids = {}
    for level, scope, rate in ladder:
        created = await new_rate(
            client, owner, org, kind="charge", rate=rate, effective_from=FROM, **scope
        )
        assert created["level"] == level and created["rate"] == rate
        rule_ids[level] = created["id"]

    def expect(found: dict, rate: int, source: str, selected_index: int) -> None:
        assert (found["charge_rate"], found["charge_source"]) == (rate, source), found
        steps = found["charge_ladder"]
        assert [i for i, s in enumerate(steps) if s["selected"]] == [selected_index]
        assert not any(s["exists"] for s in steps[:selected_index])

    # Each pass takes the top rule away and the next level shows through.
    for index, (level, _, rate) in enumerate(ladder[:4]):
        found = await resolved(client, owner, org, person, job_id=job.id)
        expect(found, rate, level, min(index, 2))
        assert (
            await client.delete(org_url(org, f"/rates/{rule_ids[level]}"), headers=owner.headers)
        ).status_code == 200
    # 4. the job's own rate (not dated, not a rule).
    found = await resolved(client, owner, org, person, job_id=job.id)
    expect(found, 6_400, "job", 3)
    assert found["charge_ladder"][3]["origin"] == "job"
    await sql("UPDATE jobs SET hourly_rate = NULL WHERE id = :j", j=str(job.id))
    for index, (level, _, rate) in enumerate(ladder[4:], start=4):
        found = await resolved(client, owner, org, person, job_id=job.id)
        expect(found, rate, level, index)
        await client.delete(org_url(org, f"/rates/{rule_ids[level]}"), headers=owner.headers)
    # 7. the grade's default rate, then the practice default.
    found = await resolved(client, owner, org, person, job_id=job.id)
    expect(found, 3_000, "grade", 6)
    assert found["charge_ladder"][6]["origin"] == "grade_default"
    await set_grade_rates(org, "SNR", charge=None, cost=700)
    found = await resolved(client, owner, org, person, job_id=job.id)
    expect(found, 2_000, "default", 7)
    assert found["charge_ladder"][7]["origin"] == "practice_default"


async def test_every_level_prices_time_and_records_its_source(client: AsyncClient) -> None:
    """Time entries snapshot the rate: one client, job and person per level."""
    owner, org = await practice(client)
    await set_practice(org, default_charge_rate=2_000)
    snr = await grade_id(org, "SNR")
    jnr = await grade_id(org, "JNR")
    svc, other_svc = await service_id(org, "ACCS"), await service_id(org, "VAT")
    senior = await staff(org)  # grade SNR, will have its own staff rate
    junior = await staff(org)  # grade JNR, only the grade rule
    plain = await staff(org)  # no grade at all
    await set_grade(org, senior, "SNR")
    await set_grade(org, junior, "JNR")

    async def charge(person, *, client_row=None, job=None, minutes=60):
        body = {"minutes": minutes}
        if job is not None:
            body["job_id"] = job.id
        elif client_row is not None:
            body["client_id"] = client_row.id
        return await log_time(client, person, org, **body)

    c1, c2, c3, c4, c5, c6, c7 = [await make_client(org) for _ in range(7)]
    j1 = await make_job(org, c1, service_id=svc)
    j2 = await make_job(org, c2, service_id=svc)
    j3 = await make_job(org, c3)
    j4 = await make_job(org, c4, hourly_rate=6_400)
    j5 = await make_job(org, c5, service_id=other_svc)
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        client_id=c1.id,
        service_id=svc,
        user_id=senior.id,
        rate=9_100,
        effective_from=FROM,
    )
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        client_id=c2.id,
        service_id=svc,
        rate=8_200,
        effective_from=FROM,
    )
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        client_id=c3.id,
        user_id=senior.id,
        rate=7_300,
        effective_from=FROM,
    )
    await new_rate(
        client, owner, org, kind="charge", client_id=c6.id, rate=7_000, effective_from=FROM
    )
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        service_id=other_svc,
        grade_id=snr,
        rate=5_500,
        effective_from=FROM,
    )
    await new_rate(
        client, owner, org, kind="charge", user_id=senior.id, rate=4_600, effective_from=FROM
    )
    await new_rate(client, owner, org, kind="charge", grade_id=jnr, rate=3_700, effective_from=FROM)

    cases = [
        (await charge(senior, job=j1), 9_100, "client_service_staff"),
        (await charge(plain, job=j2), 8_200, "client_service"),
        (await charge(senior, job=j3), 7_300, "client_staff"),
        (await charge(plain, client_row=c6), 7_000, "client"),
        (await charge(plain, job=j4), 6_400, "job"),
        (await charge(senior, job=j5), 5_500, "service_grade"),
        (await charge(senior, client_row=c7), 4_600, "staff"),
        (await charge(junior, client_row=c7), 3_700, "grade"),
        (await charge(plain, client_row=c7), 2_000, "default"),
    ]
    for entry, rate, source in cases:
        assert (entry["charge_rate"], entry["rate_source"]) == (rate, source), entry
        assert entry["charge_value"] == rate  # one hour
        assert (await entry_row(entry["id"])).rate_source == source


async def test_cost_ladder_staff_then_grade_then_default_then_zero(client: AsyncClient) -> None:
    owner, org, person, acme, _svc, _job = await world(client)
    snr = await grade_id(org, "SNR")
    await set_grade_rates(org, "SNR", cost=700)
    staff_cost = await new_rate(
        client, owner, org, kind="cost", user_id=person.id, rate=1_100, effective_from=FROM
    )
    grade_cost = await new_rate(
        client, owner, org, kind="cost", grade_id=snr, rate=900, effective_from=FROM
    )

    async def cost() -> tuple[int, str]:
        found = await resolved(client, owner, org, person)
        return found["cost_rate"], found["cost_source"]

    assert await cost() == (1_100, "staff")
    await client.delete(org_url(org, f"/rates/{staff_cost['id']}"), headers=owner.headers)
    assert await cost() == (900, "grade")
    await client.delete(org_url(org, f"/rates/{grade_cost['id']}"), headers=owner.headers)
    assert await cost() == (700, "grade")
    await set_grade_rates(org, "SNR", cost=None)
    assert await cost() == (0, "none")

    # The cost of a logged entry is snapshotted the same way.
    await new_rate(
        client, owner, org, kind="cost", user_id=person.id, rate=1_200, effective_from=FROM
    )
    entry = await log_time(client, person, org, client_id=acme.id, minutes=30)
    row = await entry_row(entry["id"])
    assert (row.cost_rate, row.cost_value) == (1_200, 600)


async def test_cost_rates_exist_only_for_staff_and_grades(client: AsyncClient) -> None:
    owner, org, person, acme, svc, _job = await world(client)
    for scope in ({"client_id": acme.id}, {"service_id": svc, "client_id": acme.id}):
        error = await rate_error(
            client, owner, org, 422, kind="cost", rate=100, effective_from=FROM, **scope
        )
        assert error["details"][0]["field"] == "kind"
    # A scope that is not one of the seven levels, or no scope at all.
    for scope in ({}, {"user_id": person.id, "grade_id": uuid.uuid4()}, {"service_id": svc}):
        error = await rate_error(
            client, owner, org, 422, kind="charge", rate=100, effective_from=FROM, **scope
        )
        assert error["details"][0]["field"] in ("scope", "grade_id")


# --- Periods ------------------------------------------------------------------------------


async def test_effective_dates_decide_which_rule_applies(client: AsyncClient) -> None:
    owner, org, person, *_ = await world(client)
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        rate=4_000,
        effective_from="2026-01-01",
        effective_to="2026-06-30",
    )
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        rate=5_000,
        effective_from="2026-07-01",
    )

    async def rate_on(day: str) -> tuple[int, str]:
        found = await client.get(
            org_url(org, "/rates/resolve"),
            headers=owner.headers,
            params={"user_id": str(person.id), "date": day},
        )
        data = ok_data(found)
        return data["charge_rate"], data["charge_source"]

    assert await rate_on("2025-12-31") == (2_000, "default")
    assert await rate_on("2026-01-01") == (4_000, "staff")
    assert await rate_on("2026-06-30") == (4_000, "staff")
    assert await rate_on("2026-07-01") == (5_000, "staff")
    assert await rate_on("2031-01-01") == (5_000, "staff")


async def test_overlapping_periods_are_refused(client: AsyncClient) -> None:
    owner, org, person, *_ = await world(client)
    other = await staff(org)
    base = await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        rate=4_000,
        effective_from="2026-01-01",
        effective_to="2026-06-30",
    )
    for start, end in (
        ("2026-06-30", None),  # shares the last day
        ("2025-12-01", "2026-01-01"),  # shares the first day
        ("2026-02-01", "2026-03-01"),  # inside
        ("2025-01-01", None),  # open-ended, runs through it
        ("2025-01-01", "2026-12-31"),  # around it
    ):
        error = await rate_error(
            client,
            owner,
            org,
            409,
            kind="charge",
            user_id=person.id,
            rate=1,
            effective_from=start,
            effective_to=end,
        )
        assert error["code"] == "RATE_OVERLAP", (start, end)
        assert [c["id"] for c in error["details"]["conflicts"]] == [base["id"]]
    assert await rule_count(org, user_id=str(person.id)) == 1
    # Next to it, another kind, another person: fine.
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        rate=4_100,
        effective_from="2026-07-01",
    )
    await new_rate(
        client, owner, org, kind="cost", user_id=person.id, rate=100, effective_from="2026-01-01"
    )
    await new_rate(
        client, owner, org, kind="charge", user_id=other.id, rate=100, effective_from="2026-01-01"
    )


async def test_invalid_periods_and_amounts_are_422(client: AsyncClient) -> None:
    owner, org, person, *_ = await world(client)
    for body in (
        {"effective_from": "2026-06-01", "effective_to": "2026-05-31", "rate": 1},
        {"effective_from": "2026-06-01", "rate": -1},
        {"effective_from": "not a date", "rate": 1},
        {"rate": 1},
        {"effective_from": "2026-06-01", "rate": 10**13},
    ):
        response = await client.post(
            org_url(org, "/rates"),
            headers=owner.headers,
            json={"kind": "charge", "user_id": str(person.id), **body},
        )
        assert response.status_code == 422, body
    assert await rule_count(org) == 0


async def test_close_previous_shortens_the_rule_running_into_the_new_one(
    client: AsyncClient,
) -> None:
    owner, org, person, *_ = await world(client)
    old = await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        rate=4_000,
        effective_from="2026-01-01",
    )
    error = await rate_error(
        client,
        owner,
        org,
        409,
        kind="charge",
        user_id=person.id,
        rate=5_000,
        effective_from="2026-07-01",
    )
    assert error["code"] == "RATE_OVERLAP"
    created = await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        rate=5_000,
        effective_from="2026-07-01",
        close_previous=True,
    )
    assert created["effective_to"] is None
    shortened = ok_data(
        await client.get(org_url(org, f"/rates/{old['id']}"), headers=owner.headers)
    )
    assert shortened["effective_to"] == "2026-06-30"
    # A bounded rule inside the old one would split it: not a plain shortening.
    error = await rate_error(
        client,
        owner,
        org,
        409,
        kind="charge",
        user_id=person.id,
        rate=1,
        effective_from="2026-02-01",
        effective_to="2026-02-28",
        close_previous=True,
    )
    assert error["code"] == "RATE_OVERLAP"


async def test_concurrent_overlapping_rates_only_one_wins(client: AsyncClient) -> None:
    owner, org, person, *_ = await world(client)
    body = {"kind": "charge", "user_id": str(person.id), "rate": 4_000, "effective_from": FROM}
    responses = await asyncio.gather(
        *[client.post(org_url(org, "/rates"), headers=owner.headers, json=body) for _ in range(6)]
    )
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409, 409, 409]
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {
        "RATE_OVERLAP"
    }
    assert await rule_count(org, user_id=str(person.id)) == 1


async def test_the_database_refuses_overlapping_rules_whatever_the_application_does(
    client: AsyncClient,
) -> None:
    owner, org, person, *_ = await world(client)
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        rate=4_000,
        effective_from=FROM,
        effective_to="2026-06-30",
    )
    insert = (
        "INSERT INTO rate_rules (id, organization_id, kind, level, user_id, rate, effective_from, "
        "effective_to) VALUES (gen_random_uuid(), :o, 'charge', 'staff', :u, 1, :f, :t)"
    )
    with pytest.raises(IntegrityError) as caught:
        await sql(insert, o=org["id"], u=str(person.id), f=date(2026, 6, 30), t=None)
    assert "ex_rate_rules_scope_period" in str(caught.value)
    # An adjacent period is fine.
    await sql(insert, o=org["id"], u=str(person.id), f=date(2026, 7, 1), t=None)


# --- Used rates and snapshots ---------------------------------------------------------------


async def test_a_used_rate_can_be_end_dated_but_not_deleted_or_moved(client: AsyncClient) -> None:
    owner, org, person, acme, *_ = await world(client)
    rule = await new_rate(
        client, owner, org, kind="charge", user_id=person.id, rate=4_000, effective_from=FROM
    )
    unused = await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        rate=4_000,
        effective_from="2030-01-01",
        close_previous=True,
    )
    assert rule["is_used"] is False and rule["can_delete"] is True
    entry = await log_time(client, person, org, client_id=acme.id, entry_date=DAY)
    assert entry["charge_rate"] == 4_000

    detail = ok_data(await client.get(org_url(org, f"/rates/{rule['id']}"), headers=owner.headers))
    assert detail["is_used"] is True and detail["can_delete"] is False
    url = org_url(org, f"/rates/{rule['id']}")
    refused = await client.delete(url, headers=owner.headers)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "RATE_IN_USE"
    moved = await client.patch(url, headers=owner.headers, json={"effective_from": "2026-02-01"})
    assert moved.status_code == 409 and moved.json()["error"]["code"] == "RATE_IN_USE"
    assert await rule_count(org, id=rule["id"]) == 1

    ended = await client.patch(url, headers=owner.headers, json={"effective_to": "2026-12-31"})
    assert ok_data(ended)["effective_to"] == "2026-12-31"
    assert ok_data(ended)["status"] in ("current", "ended")
    # An unused rule is deleted outright.
    assert (
        await client.delete(org_url(org, f"/rates/{unused['id']}"), headers=owner.headers)
    ).status_code == 200
    assert (
        await client.get(org_url(org, f"/rates/{unused['id']}"), headers=owner.headers)
    ).status_code == 404


async def test_a_rate_used_only_by_another_scope_stays_deletable(client: AsyncClient) -> None:
    owner, org, person, acme, *_ = await world(client)
    other = await staff(org)
    mine = await new_rate(
        client, owner, org, kind="charge", user_id=other.id, rate=4_000, effective_from=FROM
    )
    await log_time(client, person, org, client_id=acme.id, entry_date=DAY)
    assert (
        await client.delete(org_url(org, f"/rates/{mine['id']}"), headers=owner.headers)
    ).status_code == 200


async def test_changing_a_rate_never_rewrites_recorded_time(client: AsyncClient) -> None:
    owner, org, person, acme, *_ = await world(client)
    rule = await new_rate(
        client, owner, org, kind="charge", user_id=person.id, rate=4_000, effective_from=FROM
    )
    entry = await log_time(client, person, org, client_id=acme.id, minutes=90, entry_date=DAY)
    assert (entry["charge_rate"], entry["charge_value"]) == (4_000, 6_000)

    await client.patch(
        org_url(org, f"/rates/{rule['id']}"), headers=owner.headers, json={"rate": 9_000}
    )
    await new_rate(
        client, owner, org, kind="charge", client_id=acme.id, rate=12_000, effective_from=FROM
    )
    await set_grade_rates(org, "SNR", charge=1)
    row = await entry_row(entry["id"])
    assert (row.charge_rate, row.charge_value, row.rate_source) == (4_000, 6_000, "staff")
    fetched = ok_data(
        await client.get(org_url(org, f"/time-entries/{entry['id']}"), headers=owner.headers)
    )
    assert fetched["charge_rate"] == 4_000 and fetched["rate_source"] == "staff"


async def test_editing_an_unbilled_entry_takes_its_rate_from_the_ladder_again(
    client: AsyncClient,
) -> None:
    owner, org, person, acme, *_ = await world(client)
    rule = await new_rate(
        client, owner, org, kind="charge", user_id=person.id, rate=4_000, effective_from=FROM
    )
    entry = await log_time(client, person, org, client_id=acme.id, minutes=60, entry_date=DAY)
    await client.patch(
        org_url(org, f"/rates/{rule['id']}"), headers=owner.headers, json={"rate": 9_000}
    )
    assert (await entry_row(entry["id"])).charge_rate == 4_000
    # The V1 rule stays: a change to the entry itself re-takes the rate while it is unbilled.
    edited = ok_data(
        await client.patch(
            org_url(org, f"/time-entries/{entry['id']}"),
            headers=person.headers,
            json={"minutes": 90},
        )
    )
    assert (edited["charge_rate"], edited["charge_value"], edited["rate_source"]) == (
        9_000,
        13_500,
        "staff",
    )


async def test_updating_a_rate_validates_overlap_and_reports_changes(client: AsyncClient) -> None:
    owner, org, person, *_ = await world(client)
    first = await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        rate=4_000,
        effective_from="2026-01-01",
        effective_to="2026-06-30",
    )
    second = await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        rate=5_000,
        effective_from="2026-07-01",
    )
    response = await client.patch(
        org_url(org, f"/rates/{first['id']}"),
        headers=owner.headers,
        json={"effective_to": "2026-07-15"},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "RATE_OVERLAP"
    # Null makes it open-ended again (which also overlaps the next one), unknown ids are 404.
    response = await client.patch(
        org_url(org, f"/rates/{second['id']}"),
        headers=owner.headers,
        json={"effective_to": "2026-12-31", "rate": 5_500},
    )
    body = ok_data(response)
    assert (body["rate"], body["effective_to"]) == (5_500, "2026-12-31")
    response = await client.patch(
        org_url(org, f"/rates/{uuid.uuid4()}"), headers=owner.headers, json={"rate": 1}
    )
    assert response.status_code == 404


# --- History ---------------------------------------------------------------------------------


async def test_history_lists_every_change_newest_first_with_old_and_new(
    client: AsyncClient,
) -> None:
    owner, org, person, *_ = await world(client)
    snr = await grade_id(org, "SNR")
    rule = await new_rate(
        client, owner, org, kind="charge", user_id=person.id, rate=4_000, effective_from=FROM
    )
    url = org_url(org, f"/rates/{rule['id']}")
    await client.patch(url, headers=owner.headers, json={"rate": 4_500})
    await client.patch(url, headers=owner.headers, json={"effective_to": "2026-12-31"})
    await client.delete(url, headers=owner.headers)
    await new_rate(client, owner, org, kind="cost", grade_id=snr, rate=800, effective_from=FROM)

    page = ok_data(await client.get(org_url(org, "/rates/history"), headers=owner.headers))
    assert [c["action"] for c in page] == ["created", "deleted", "end_dated", "updated", "created"]
    created, deleted, end_dated, updated, first = page
    assert first["old"] is None and first["new"] == {
        "rate": 4_000,
        "effective_from": FROM,
        "effective_to": None,
    }
    assert (updated["old"]["rate"], updated["new"]["rate"]) == (4_000, 4_500)
    assert (end_dated["old"]["effective_to"], end_dated["new"]["effective_to"]) == (
        None,
        "2026-12-31",
    )
    assert deleted["new"] is None and deleted["old"]["rate"] == 4_500
    assert deleted["staff"]["id"] == str(person.id) and deleted["actor_id"] == str(owner.id)
    assert created["grade"]["name"] == "Senior" and created["kind"] == "cost"
    assert all(c["source"] == "rates" and c["actor_name"] for c in page)

    # Filters.
    def ids(rows):
        return [c["action"] for c in rows]

    async def hist(**params):
        return ok_data(
            await client.get(org_url(org, "/rates/history"), headers=owner.headers, params=params)
        )

    assert len(await hist(user_id=str(person.id))) == 4
    assert ids(await hist(grade_id=snr)) == ["created"]
    assert ids(await hist(kind="cost")) == ["created"]
    assert ids(await hist(action="updated")) == ["updated"]
    assert await hist(date_from="2030-01-01") == []
    assert len(await hist(date_from="2020-01-01", date_to="2099-01-01")) == 5
    # Pagination is stable.
    first_page = await client.get(
        org_url(org, "/rates/history"), headers=owner.headers, params={"limit": 2}
    )
    cursor = first_page.json()["meta"]["next_cursor"]
    rest = await client.get(
        org_url(org, "/rates/history"),
        headers=owner.headers,
        params={"limit": 10, "cursor": cursor},
    )
    assert len(first_page.json()["data"]) + len(rest.json()["data"]) == 5
    assert (await audit_entries(org["id"], "rate.deleted"))[0]["metadata"]["kind"] == "charge"
    assert len(await audit_entries(org["id"], "rate.created")) == 2


async def test_v1_staff_and_client_rates_are_rules_too(client: AsyncClient) -> None:
    owner, org, person, acme, *_ = await world(client)
    response = await client.post(
        org_url(org, f"/members/{person.id}/rates"),
        headers=owner.headers,
        json={"effective_from": "2026-03-01", "charge_rate": 4_200, "cost_rate": 1_300},
    )
    assert response.status_code == 201, response.text
    response = await client.post(
        org_url(org, f"/clients/{acme.id}/rates"),
        headers=owner.headers,
        json={"user_id": str(person.id), "hourly_rate": 7_700},
    )
    assert response.status_code == 201, response.text
    rules = ok_data(
        await client.get(
            org_url(org, "/rates"),
            headers=owner.headers,
            params={"user_id": str(person.id), "sort": "effective_from", "order": "asc"},
        )
    )
    assert sorted((r["kind"], r["level"], r["rate"]) for r in rules) == [
        ("charge", "client_staff", 7_700),
        ("charge", "staff", 4_200),
        ("cost", "staff", 1_300),
    ]
    history = ok_data(await client.get(org_url(org, "/rates/history"), headers=owner.headers))
    assert {c["source"] for c in history} == {"staff_rates", "client_rates"}


# --- Listing, preview -------------------------------------------------------------------------


async def test_list_filters_sort_and_pagination(client: AsyncClient) -> None:
    owner, org, person, acme, svc, _ = await world(client)
    snr = await grade_id(org, "SNR")
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        rate=4_000,
        effective_from="2026-01-01",
        effective_to="2026-03-31",
    )
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        rate=4_500,
        effective_from="2026-04-01",
        effective_to="2099-12-31",
    )
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=person.id,
        rate=5_000,
        effective_from="2100-01-01",
    )
    await new_rate(
        client, owner, org, kind="cost", user_id=person.id, rate=900, effective_from="2026-01-01"
    )
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        client_id=acme.id,
        service_id=svc,
        rate=8_200,
        effective_from="2026-01-01",
    )
    await new_rate(
        client, owner, org, kind="charge", grade_id=snr, rate=3_700, effective_from="2026-01-01"
    )

    async def ls(**params):
        response = await client.get(org_url(org, "/rates"), headers=owner.headers, params=params)
        return ok_data(response)

    assert len(await ls()) == 6
    assert len(await ls(kind="cost")) == 1
    assert {r["level"] for r in await ls(level=["client_service", "grade"])} == {
        "client_service",
        "grade",
    }
    assert len(await ls(user_id=str(person.id))) == 4
    assert len(await ls(client_id=str(acme.id))) == 1 and len(await ls(grade_id=snr)) == 1
    assert len(await ls(service_id=str(svc))) == 1
    assert [r["rate"] for r in await ls(status="ended")] == [4_000]
    assert [r["rate"] for r in await ls(status="future")] == [5_000]
    assert {
        r["rate"] for r in await ls(status="current", user_id=str(person.id), kind="charge")
    } == {4_500}
    assert [
        r["rate"] for r in await ls(on_date="2026-02-01", user_id=str(person.id), kind="charge")
    ] == [4_000]
    assert [
        r["rate"] for r in await ls(user_id=str(person.id), kind="charge", sort="rate", order="asc")
    ] == [4_000, 4_500, 5_000]
    assert len(await ls(q="Senior")) >= 1
    # Pages of two, every rule exactly once.
    seen = []
    cursor = None
    while True:
        params = {"limit": 2, "sort": "rate", "order": "desc"} | (
            {"cursor": cursor} if cursor else {}
        )
        response = await client.get(org_url(org, "/rates"), headers=owner.headers, params=params)
        seen += [r["id"] for r in response.json()["data"]]
        cursor = response.json()["meta"].get("next_cursor")
        if not cursor:
            break
    assert len(seen) == 6 and len(set(seen)) == 6
    first = (await ls())[0]
    assert {
        "id",
        "kind",
        "level",
        "level_label",
        "precedence",
        "client",
        "service",
        "staff",
        "grade",
        "rate",
        "effective_from",
        "effective_to",
        "status",
        "is_used",
        "can_delete",
    } <= set(first)


async def test_rate_lists_cost_the_same_number_of_queries_whatever_their_size(
    client: AsyncClient,
) -> None:
    owner, org, person, acme, svc, _ = await world(client)

    async def add(count: int, first: int) -> None:
        for number in range(first, first + count):
            start = f"{2000 + number}-01-01"
            await new_rate(
                client,
                owner,
                org,
                kind="charge",
                user_id=person.id,
                rate=1_000 + number,
                effective_from=start,
                effective_to=start.replace("-01-01", "-12-31"),
            )
            await new_rate(
                client,
                owner,
                org,
                kind="charge",
                client_id=acme.id,
                service_id=svc,
                rate=2_000 + number,
                effective_from=start,
                effective_to=start.replace("-01-01", "-12-31"),
            )

    async def read(path: str) -> list[dict]:
        return ok_data(
            await client.get(org_url(org, path), headers=owner.headers, params={"limit": 100})
        )

    await add(2, 0)
    with count_queries() as few_rules:
        await read("/rates")
    with count_queries() as few_changes:
        await read("/rates/history")
    await add(8, 2)
    with count_queries() as many_rules:
        rules = await read("/rates")
    with count_queries() as many_changes:
        changes = await read("/rates/history")
    assert len(rules) >= 20 and len(changes) >= 20
    assert len(many_rules) == len(few_rules) and len(many_changes) == len(few_changes)


async def test_resolve_with_what_if_service_and_unknown_ids(client: AsyncClient) -> None:
    owner, org, person, acme, svc, job = await world(client)
    other_svc = await service_id(org, "VAT")
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        client_id=acme.id,
        service_id=other_svc,
        rate=8_800,
        effective_from=FROM,
    )
    base = await resolved(client, owner, org, person, client_id=acme.id)
    assert base["charge_source"] != "client_service"
    what_if = await resolved(client, owner, org, person, client_id=acme.id, service_id=other_svc)
    assert (what_if["charge_rate"], what_if["charge_source"]) == (8_800, "client_service")
    assert what_if["service"]["id"] == str(other_svc) and what_if["client"]["id"] == str(acme.id)
    assert what_if["staff"]["id"] == str(person.id) and what_if["grade"]["code"] == "SNR"
    # The job supplies the client and the service.
    via_job = await resolved(client, owner, org, person, job_id=job.id)
    assert via_job["client"]["id"] == str(acme.id) and via_job["service"]["id"] == str(svc)
    for params in (
        {"user_id": str(uuid.uuid4())},
        {"user_id": str(person.id), "client_id": str(uuid.uuid4())},
        {"user_id": str(person.id), "job_id": str(uuid.uuid4())},
        {"user_id": str(person.id), "service_id": str(uuid.uuid4())},
    ):
        response = await client.get(
            org_url(org, "/rates/resolve"), headers=owner.headers, params=params
        )
        assert response.status_code == 404, params
    # A job of another client is a validation error.
    beta = await make_client(org)
    response = await client.get(
        org_url(org, "/rates/resolve"),
        headers=owner.headers,
        params={"user_id": str(person.id), "job_id": str(job.id), "client_id": str(beta.id)},
    )
    assert response.status_code == 422


# --- Permissions, cost leakage, tenants -------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "can_read", "can_manage"),
    [
        ("owner", True, True),
        ("admin", True, True),
        ("partner", True, True),
        ("manager", True, False),
        ("staff", False, False),
        ("billing", False, False),
        ("viewer", False, False),
    ],
)
async def test_permissions_per_role(
    client: AsyncClient, role: str, can_read: bool, can_manage: bool
) -> None:
    owner, org, person, *_ = await world(client)
    rule = await new_rate(
        client, owner, org, kind="charge", user_id=person.id, rate=4_000, effective_from=FROM
    )
    actor = owner if role == "owner" else await staff(org, role)
    reads = [
        client.get(org_url(org, "/rates"), headers=actor.headers),
        client.get(org_url(org, f"/rates/{rule['id']}"), headers=actor.headers),
        client.get(org_url(org, "/rates/history"), headers=actor.headers),
        client.get(
            org_url(org, "/rates/resolve"),
            headers=actor.headers,
            params={"user_id": str(person.id)},
        ),
        client.get(org_url(org, "/rates/reruns"), headers=actor.headers),
    ]
    for response in await asyncio.gather(*reads):
        assert response.status_code == (200 if can_read else 403), response.text
    created = await client.post(
        org_url(org, "/rates"),
        headers=actor.headers,
        json={
            "kind": "charge",
            "grade_id": await grade_id(org, "MGR"),
            "rate": 100,
            "effective_from": FROM,
        },
    )
    assert created.status_code == (201 if can_manage else 403), created.text
    changed = await client.patch(
        org_url(org, f"/rates/{rule['id']}"), headers=actor.headers, json={"rate": 4_100}
    )
    assert changed.status_code == (200 if can_manage else 403)
    scope = {"date_from": "2026-01-01", "date_to": "2026-01-31"}
    preview = await client.post(
        org_url(org, "/rates/rerate/preview"), headers=actor.headers, json={"scope": scope}
    )
    assert preview.status_code == (200 if can_manage else 403), preview.text
    deleted = await client.delete(org_url(org, f"/rates/{rule['id']}"), headers=actor.headers)
    assert deleted.status_code == (200 if can_manage else 403)


async def test_no_cost_rate_leaks_without_rate_read(client: AsyncClient) -> None:
    _owner, org, person, acme, *_ = await world(client)
    editor_role = await add_custom_role(
        org["id"], "Rate editor", ["member.read", "rate.manage", "organization.read"]
    )
    assert editor_role
    editor = await add_member(org["id"], "Rate editor")
    # Can change rates but never sees a cost amount, in any response.
    cost = await client.post(
        org_url(org, "/rates"),
        headers=editor.headers,
        json={"kind": "cost", "user_id": str(person.id), "rate": 1_234, "effective_from": FROM},
    )
    assert cost.status_code == 201 and cost.json()["data"]["rate"] is None
    assert "1234" not in cost.text
    charge = await client.post(
        org_url(org, "/rates"),
        headers=editor.headers,
        json={"kind": "charge", "user_id": str(person.id), "rate": 4_000, "effective_from": FROM},
    )
    assert charge.status_code == 201 and charge.json()["data"]["rate"] == 4_000
    patched = await client.patch(
        org_url(org, f"/rates/{cost.json()['data']['id']}"),
        headers=editor.headers,
        json={"rate": 1_300},
    )
    assert patched.status_code == 200 and patched.json()["data"]["rate"] is None
    for path in ("/rates", "/rates/history", "/rates/reruns"):
        assert (await client.get(org_url(org, path), headers=editor.headers)).status_code == 403
    # Logged time hides cost without rate.read too; with it the manager sees them.
    entry = await log_time(client, person, org, client_id=acme.id, minutes=60)
    reader = await staff(org, "manager")
    plain = await staff(org, "staff")
    seen = ok_data(
        await client.get(
            org_url(org, "/time-entries"), headers=reader.headers, params={"user_id": "all"}
        )
    )
    assert next(e for e in seen if e["id"] == entry["id"])["cost_rate"] is not None
    assert (
        await client.get(
            org_url(org, "/time-entries"), headers=plain.headers, params={"user_id": "all"}
        )
    ).status_code == 403
    mine = ok_data(await client.get(org_url(org, "/time-entries"), headers=person.headers))
    assert mine[0]["cost_rate"] is None and mine[0]["cost_value"] is None
    # The staff list and the rate preview are masked the same way.
    listing = ok_data(await client.get(org_url(org, "/staff"), headers=plain.headers))
    assert all(s["cost_rate"] is None and s["charge_rate"] is None for s in listing)
    resolve = await client.get(
        org_url(org, "/rates/resolve"), headers=reader.headers, params={"user_id": str(person.id)}
    )
    assert ok_data(resolve)["cost_rate"] is not None


async def test_other_practices_rates_are_invisible(client: AsyncClient) -> None:
    owner, org, _person, _acme, _svc, _ = await world(client)
    _, other = await practice(client, "Other & Co")
    other_staff = await staff(other)
    their_client = await make_client(other)
    rule = await make_staff_rate(other, other_staff, 9_900)
    url = org_url(org, f"/rates/{rule.id}")
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    assert (await client.patch(url, headers=owner.headers, json={"rate": 1})).status_code == 404
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    assert ok_data(await client.get(org_url(org, "/rates"), headers=owner.headers)) == []
    # Their people, clients, grades and services cannot be named in a rate of ours.
    their_grade = await grade_id(other, "SNR")
    for scope in (
        {"user_id": str(other_staff.id)},
        {"client_id": str(their_client.id)},
        {"grade_id": their_grade},
        {"service_id": str(await service_id(other, "ACCS"))},
    ):
        response = await client.post(
            org_url(org, "/rates"),
            headers=owner.headers,
            json={"kind": "charge", "rate": 1, "effective_from": FROM, **scope},
        )
        assert response.status_code in (404, 422), scope
    assert await rule_count(org) == 0
    # ... nor resolved.
    response = await client.get(
        org_url(org, "/rates/resolve"),
        headers=owner.headers,
        params={"user_id": str(other_staff.id)},
    )
    assert response.status_code == 404
    # Someone who is not a member of the practice gets 404 for the whole area.
    outsider = (await practice(client, "Fourth"))[0]
    assert (await client.get(org_url(org, "/rates"), headers=outsider.headers)).status_code == 404


async def test_a_person_who_left_cannot_be_given_a_rate(client: AsyncClient) -> None:
    owner, org, _person, *_ = await world(client)
    gone = await staff(org, status="removed")
    response = await client.post(
        org_url(org, "/rates"),
        headers=owner.headers,
        json={"kind": "charge", "user_id": str(gone.id), "rate": 1, "effective_from": FROM},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "MEMBER_REMOVED"

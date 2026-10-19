"""The practice's rate mode: ``staff_first`` (the default) or ``grade_only``, which skips the
staff-level rate (level 6 of the charge-out ladder) so the grade rate applies. The resolver, the
rate preview, time entries, the staff list and re-rating all use it; levels 1-5 and the cost
ladder do not change."""

from datetime import date

from httpx import AsyncClient

from tests.clients_helpers import sql
from tests.domain_factories import (
    make_client,
    make_job,
    make_time_entry,
    practice,
    service_id,
    staff,
)
from tests.factories import audit_entries
from tests.team_helpers import grade_id, new_rate, set_grade, set_grade_rates
from tests.time_helpers import entry_row, log_time, ok_data, org_url, set_practice

DAY = "2026-09-07"
FROM = "2026-01-01"


async def world(client: AsyncClient):
    """A senior with a personal rate (level 6, 4 600), a grade rate (level 7, 3 700) and a
    practice default (2 000), on a job that has a service and no rate of its own."""
    owner, org = await practice(client)
    await set_practice(org, default_charge_rate=2_000)
    person = await staff(org)
    await set_grade(org, person, "SNR")
    acme = await make_client(org)
    svc = await service_id(org, "ACCS")
    job = await make_job(org, acme, service_id=svc)
    snr = await grade_id(org, "SNR")
    await new_rate(
        client, owner, org, kind="charge", user_id=person.id, rate=4_600, effective_from=FROM
    )
    await new_rate(client, owner, org, kind="charge", grade_id=snr, rate=3_700, effective_from=FROM)
    return owner, org, person, acme, svc, job


async def resolved(client: AsyncClient, actor, org: dict, person, **params) -> dict:
    query = {"user_id": str(person.id), "date": DAY} | {k: str(v) for k, v in params.items()}
    return ok_data(
        await client.get(org_url(org, "/rates/resolve"), headers=actor.headers, params=query)
    )


async def switch(client: AsyncClient, actor, org: dict, mode: str):
    return await client.patch(
        f"/api/v1/organizations/{org['id']}", headers=actor.headers, json={"rate_mode": mode}
    )


async def test_a_new_practice_prices_staff_first(client: AsyncClient) -> None:
    owner, org, person, *_ = await world(client)
    assert org["rate_mode"] == "staff_first"
    found = await resolved(client, owner, org, person)
    assert (found["charge_rate"], found["charge_source"]) == (4_600, "staff")
    steps = found["charge_ladder"]
    assert [i for i, s in enumerate(steps) if s["selected"]] == [5]
    assert steps[5]["applicable"] is True


async def test_grade_only_skips_the_staff_rate_and_the_grade_rate_applies(
    client: AsyncClient,
) -> None:
    owner, org, person, *_ = await world(client)
    response = await switch(client, owner, org, "grade_only")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["rate_mode"] == "grade_only"

    found = await resolved(client, owner, org, person)
    assert (found["charge_rate"], found["charge_source"]) == (3_700, "grade")
    steps = found["charge_ladder"]
    assert [i for i, s in enumerate(steps) if s["selected"]] == [6]
    # Level 6 is shown, not applicable, and says why; the staff rule still exists as data.
    assert steps[5]["applicable"] is False and steps[5]["exists"] is False
    assert "grade only" in steps[5]["reason"]

    # The grade rate is the only thing left to fall back on: the personal rate never shows through.
    set_to = await sql("SELECT id FROM rate_rules WHERE level = 'grade'")
    await sql("DELETE FROM rate_rules WHERE id = :i", i=str(set_to[0]["id"]))
    await set_grade_rates(org, "SNR", charge=3_000)
    found = await resolved(client, owner, org, person)
    assert (found["charge_rate"], found["charge_source"]) == (3_000, "grade")
    await set_grade_rates(org, "SNR", charge=None)
    found = await resolved(client, owner, org, person)
    assert (found["charge_rate"], found["charge_source"]) == (2_000, "default")

    # Back to staff first: the personal rate applies again.
    assert (await switch(client, owner, org, "staff_first")).status_code == 200
    found = await resolved(client, owner, org, person)
    assert (found["charge_rate"], found["charge_source"]) == (4_600, "staff")


async def test_levels_one_to_five_are_the_same_in_both_modes(client: AsyncClient) -> None:
    owner, org, person, acme, svc, job = await world(client)
    snr = await grade_id(org, "SNR")
    await sql("UPDATE jobs SET hourly_rate = 6_400 WHERE id = :j", j=str(job.id))
    ladder = [
        (
            "client_service_staff",
            {"client_id": acme.id, "service_id": svc, "user_id": person.id},
            9_100,
        ),
        ("client_service", {"client_id": acme.id, "service_id": svc}, 8_200),
        ("client_staff", {"client_id": acme.id, "user_id": person.id}, 7_300),
        ("client", {"client_id": acme.id}, 7_000),
        ("job", None, 6_400),
        ("service_grade", {"service_id": svc, "grade_id": snr}, 5_500),
    ]
    rule_ids = {}
    for source, scope, rate in ladder:
        if scope is not None:
            made = await new_rate(
                client, owner, org, kind="charge", rate=rate, effective_from=FROM, **scope
            )
            rule_ids[source] = made["id"]
    for source, _scope, rate in ladder:
        for mode in ("staff_first", "grade_only"):
            await set_practice(org, rate_mode=mode)
            found = await resolved(client, owner, org, person, job_id=job.id)
            assert (found["charge_rate"], found["charge_source"]) == (rate, source), (mode, source)
        if source == "job":
            await sql("UPDATE jobs SET hourly_rate = NULL WHERE id = :j", j=str(job.id))
        else:
            gone = await client.delete(
                org_url(org, f"/rates/{rule_ids[source]}"), headers=owner.headers
            )
            assert gone.status_code == 200, gone.text
    # Nothing above the staff level is left: now the two modes differ.
    await set_practice(org, rate_mode="staff_first")
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (4_600, "staff")
    await set_practice(org, rate_mode="grade_only")
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (3_700, "grade")


async def test_the_cost_rate_is_unaffected_by_the_mode(client: AsyncClient) -> None:
    owner, org, person, *_ = await world(client)
    await new_rate(
        client, owner, org, kind="cost", user_id=person.id, rate=1_500, effective_from=FROM
    )
    for mode in ("staff_first", "grade_only"):
        await set_practice(org, rate_mode=mode)
        found = await resolved(client, owner, org, person)
        assert (found["cost_rate"], found["cost_source"]) == (1_500, "staff"), mode


async def test_time_entries_snapshot_the_rate_of_the_mode_in_force(client: AsyncClient) -> None:
    owner, org, person, _acme, _svc, job = await world(client)
    first = await log_time(client, person, org, job_id=job.id, entry_date=DAY)
    assert (first["charge_rate"], first["rate_source"]) == (4_600, "staff")

    assert (await switch(client, owner, org, "grade_only")).status_code == 200
    second = await log_time(client, person, org, job_id=job.id, entry_date=DAY)
    assert (second["charge_rate"], second["rate_source"]) == (3_700, "grade")
    assert second["charge_value"] == 3_700
    # A mode change never rewrites recorded time ...
    assert (await entry_row(first["id"])).charge_rate == 4_600
    # ... until an unbilled entry is edited and takes its rate from the ladder again.
    edited = ok_data(
        await client.patch(
            org_url(org, f"/time-entries/{first['id']}"),
            headers=person.headers,
            json={"minutes": 30},
        )
    )
    assert (edited["charge_rate"], edited["rate_source"]) == (3_700, "grade")
    assert edited["charge_value"] == 1_850


async def test_the_staff_list_shows_the_rate_that_applies(client: AsyncClient) -> None:
    owner, org, person, *_ = await world(client)

    async def shown() -> tuple[int, str]:
        rows = ok_data(await client.get(org_url(org, "/staff"), headers=owner.headers))
        [row] = [r for r in rows if r["id"] == str(person.id)]
        return row["charge_rate"], row["charge_rate_source"]

    assert await shown() == (4_600, "staff")
    await set_practice(org, rate_mode="grade_only")
    assert await shown() == (3_700, "grade")


async def test_rerating_prices_by_the_mode_and_previews_it(client: AsyncClient) -> None:
    owner, org, person, acme, _svc, _job = await world(client)
    entry = await make_time_entry(
        org, person, client=acme, entry_date=date(2026, 9, 7), charge_rate=4_600, cost_rate=0
    )
    scope = {"user_ids": [str(person.id)], "date_from": "2026-09-01", "date_to": "2026-09-30"}

    async def preview() -> dict:
        return ok_data(
            await client.post(
                org_url(org, "/rates/rerate/preview"), headers=owner.headers, json={"scope": scope}
            )
        )

    # Staff first prices it at 4 600 again: nothing to change.
    assert (await preview())["totals"]["entries_to_change"] == 0
    await set_practice(org, rate_mode="grade_only")
    shown = await preview()
    assert shown["totals"]["entries_to_change"] == 1
    [item] = shown["items"]
    assert (item["old"]["charge_rate"], item["new"]["charge_rate"], item["new"]["source"]) == (
        4_600,
        3_700,
        "grade",
    )
    applied = await client.post(
        org_url(org, "/rates/rerate"),
        headers=owner.headers,
        json={"scope": scope, "preview_token": shown["preview_token"]},
    )
    assert applied.status_code == 201, applied.text
    row = await entry_row(entry.id)
    assert (row.charge_rate, row.rate_source) == (3_700, "grade")


async def test_the_mode_is_a_setting_with_a_choice_of_two(client: AsyncClient) -> None:
    owner, org, *_ = await world(client)
    for bad in ("staff", "grade", "", None, 1):
        response = await client.patch(
            f"/api/v1/organizations/{org['id']}", headers=owner.headers, json={"rate_mode": bad}
        )
        assert response.status_code == 422, bad
        assert response.json()["error"]["details"][0]["field"] == "rate_mode"
    # Changing it is audited with the old and the new value.
    await switch(client, owner, org, "grade_only")
    [entry] = await audit_entries(org["id"], "organization.updated")
    assert entry["metadata"]["changes"] == {"rate_mode": ["staff_first", "grade_only"]}
    # Setting what it already is writes nothing.
    await switch(client, owner, org, "grade_only")
    assert len(await audit_entries(org["id"], "organization.updated")) == 1

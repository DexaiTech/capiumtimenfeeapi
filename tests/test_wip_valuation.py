"""WIP valuation (APT84): the practice chooses which rate sources value its time and in what
order (`organizations.wip_valuation_sources`). The rate ladder follows it, a switched-off source
never prices, "No rate" when nothing prices and the default is 0, the preview and the save (with
or without re-valuing unbilled time), the deprecated `rate_mode`, the client Jobs tab, re-rating
and the migration."""

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

from app.db.models.organization import RATE_MODE_SOURCES
from app.services import period_locks, wip_valuation
from app.services.charge_rates import GRADE_OFF_REASON, OFF_REASON
from tests.clients_helpers import sql
from tests.domain_factories import (
    make_client,
    make_job,
    make_time_entry,
    practice,
    service_id,
    staff,
)
from tests.factories import API, audit_entries
from tests.migration_helpers import head_revision, migrate
from tests.team_helpers import bill, grade_id, new_rate, set_grade, set_grade_rates
from tests.time_helpers import entry_row, err, log_time, ok_data, org_url, set_practice

DAY = "2026-09-07"
FROM = "2026-01-01"
NEW_PRACTICE = ["client", "service", "grade", "staff"]
STAFF_FIRST = list(RATE_MODE_SOURCES["staff_first"])


async def put(client: AsyncClient, actor, org: dict, sources: list, **body):
    return await client.put(
        org_url(org, "/wip-valuation"), headers=actor.headers, json={"sources": sources, **body}
    )


async def preview(client: AsyncClient, actor, org: dict, sources: list):
    return await client.post(
        org_url(org, "/wip-valuation/preview"), headers=actor.headers, json={"sources": sources}
    )


async def resolved(client: AsyncClient, actor, org: dict, person, **params) -> dict:
    query = {"user_id": str(person.id), "date": DAY} | {k: str(v) for k, v in params.items()}
    return ok_data(
        await client.get(org_url(org, "/rates/resolve"), headers=actor.headers, params=query)
    )


async def world(client: AsyncClient):
    """A senior with a staff rate (4 600) and a grade rate (3 700), a client rate for everyone
    (7 000) and a service rate (5 000), on a job of the client with that service and no rate of
    its own; practice default 2 000."""
    owner, org = await practice(client)
    await set_practice(org, default_charge_rate=2_000)
    person = await staff(org)
    await set_grade(org, person, "SNR")
    acme = await make_client(org)
    svc = await service_id(org, "ACCS")
    job = await make_job(org, acme, service_id=svc)
    for scope, rate in (
        ({"user_id": person.id}, 4_600),
        ({"grade_id": await grade_id(org, "SNR")}, 3_700),
        ({"client_id": acme.id}, 7_000),
        ({"service_id": svc}, 5_000),
    ):
        await new_rate(client, owner, org, kind="charge", rate=rate, effective_from=FROM, **scope)
    return owner, org, person, acme, svc, job


# --- The ladder follows the order --------------------------------------------------------


async def test_a_new_practice_values_client_service_grade_staff(client: AsyncClient) -> None:
    owner, org = await practice(client)
    found = ok_data(await client.get(f"{API}/organizations/{org['id']}", headers=owner.headers))
    assert found["organization"]["wip_valuation_sources"] == NEW_PRACTICE
    # The deprecated rate mode is derived: both staff and grade rates are on.
    assert found["organization"]["rate_mode"] == "staff_first"


async def test_each_order_prices_by_its_first_source_that_has_a_rate(client: AsyncClient) -> None:
    owner, org, person, _acme, _svc, job = await world(client)
    for sources, expected in (
        (NEW_PRACTICE, (7_000, "client")),
        (["service", "client", "grade", "staff"], (5_000, "service")),
        (["grade", "staff", "client", "service"], (3_700, "grade")),
        (["staff", "grade", "service", "client"], (4_600, "staff")),
        (["client"], (7_000, "client")),
        (["service"], (5_000, "service")),
        (["grade"], (3_700, "grade")),
        (["staff"], (4_600, "staff")),
        (["service", "staff"], (5_000, "service")),
        (["staff", "client"], (4_600, "staff")),
    ):
        saved = ok_data(await put(client, owner, org, sources))
        assert saved["wip_valuation_sources"] == sources
        found = await resolved(client, owner, org, person, job_id=job.id)
        assert (found["charge_rate"], found["charge_source"]) == expected, sources
        # Time saved now is priced the same way.
        entry = await log_time(client, person, org, job_id=job.id, entry_date=DAY)
        assert (entry["charge_rate"], entry["rate_source"]) == expected, sources


async def test_switched_off_sources_come_back_last_with_the_reason(client: AsyncClient) -> None:
    owner, org, person, _acme, _svc, job = await world(client)
    ok_data(await put(client, owner, org, ["staff", "service"]))
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (4_600, "staff")
    steps = found["charge_ladder"]
    assert [s["level"] for s in steps] == [6, 5, 1, 2, 3, 4, 7, None, None]
    assert [s["source"] for s in steps][-2:] == ["default", "none"]
    assert [i for i, s in enumerate(steps) if s["selected"]] == [0]
    for step in steps[2:7]:
        assert (step["applicable"], step["exists"], step["reason"]) == (False, False, OFF_REASON)
    # The client rate (7 000) and the grade rate (3 700) exist as data but price nothing.
    assert steps[1]["applicable"] is True and steps[1]["rate"] == 5_000


async def test_the_client_source_includes_the_job_s_own_rate(client: AsyncClient) -> None:
    owner, org, person, _acme, _svc, job = await world(client)
    await sql("DELETE FROM rate_rules WHERE level = 'client'")
    await sql("UPDATE jobs SET hourly_rate = 6400 WHERE id = :j", j=str(job.id))
    ok_data(await put(client, owner, org, ["client", "service"]))
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (6_400, "job")
    ok_data(await put(client, owner, org, ["service", "client"]))
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (5_000, "service")
    # The client source off: the job's own rate is off with it.
    ok_data(await put(client, owner, org, ["staff"]))
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (4_600, "staff")
    job_step = next(s for s in found["charge_ladder"] if s["level"] == 4)
    assert (job_step["applicable"], job_step["reason"]) == (False, OFF_REASON)


async def test_the_grade_rate_for_a_service_applies_only_while_grade_rates_are_on(
    client: AsyncClient,
) -> None:
    owner, org, person, _acme, svc, job = await world(client)
    await sql("DELETE FROM rate_rules WHERE level IN ('client', 'service')")
    snr = await grade_id(org, "SNR")
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
    ok_data(await put(client, owner, org, ["service", "staff", "grade"]))
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (5_500, "service_grade")

    ok_data(await put(client, owner, org, ["service", "staff"]))
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (4_600, "staff")
    level5 = next(s for s in found["charge_ladder"] if s["level"] == 5)
    assert (level5["applicable"], level5["reason"]) == (False, GRADE_OFF_REASON)

    # The person's own rate for the service applies whether staff rates are on or not.
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        service_id=svc,
        user_id=person.id,
        rate=5_200,
        effective_from=FROM,
    )
    ok_data(await put(client, owner, org, ["service", "grade"]))
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (5_200, "service_staff")


async def test_nothing_prices_the_default_does_else_no_rate(client: AsyncClient) -> None:
    owner, org, person, acme, _svc, job = await world(client)
    await sql("DELETE FROM rate_rules WHERE level = 'client'")
    ok_data(await put(client, owner, org, ["client"]))
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (2_000, "default")

    await set_practice(org, default_charge_rate=0)
    found = await resolved(client, owner, org, person, job_id=job.id)
    assert (found["charge_rate"], found["charge_source"]) == (0, "none")
    default, none = found["charge_ladder"][-2:]
    assert (default["applicable"], default["exists"]) == (False, False)
    assert (none["selected"], none["label"]) == (True, "No rate")

    # The time is valued at 0 and says "No rate"; WIP Details shows it.
    patched = await client.patch(
        f"{API}/organizations/{org['id']}",
        headers=owner.headers,
        json={"require_timesheet_approval": False},
    )
    assert patched.status_code == 200, patched.text
    entry = await log_time(client, person, org, job_id=job.id, entry_date=DAY)
    assert (entry["charge_rate"], entry["charge_value"], entry["rate_source"]) == (0, 0, "none")
    items = ok_data(
        await client.get(
            org_url(org, "/wip/items"), headers=owner.headers, params={"client_id": str(acme.id)}
        )
    )
    [item] = [i for i in items if i["id"] == entry["id"]]
    assert (item["rate"], item["rate_source"], item["value"]) == (0, "none", 0)


async def test_the_former_rate_modes_are_their_orders(client: AsyncClient) -> None:
    owner, org, person, *_ = await world(client)
    await sql("DELETE FROM rate_rules WHERE level IN ('client', 'service')")
    url = f"{API}/organizations/{org['id']}"
    for mode, expected in (
        ("grade_only", (3_700, "grade")),
        ("staff_only", (4_600, "staff")),
        ("staff_first", (4_600, "staff")),
    ):
        patched = ok_data(await client.patch(url, headers=owner.headers, json={"rate_mode": mode}))
        assert patched["rate_mode"] == mode
        assert patched["wip_valuation_sources"] == list(RATE_MODE_SOURCES[mode])
        found = await resolved(client, owner, org, person)
        assert (found["charge_rate"], found["charge_source"]) == expected, mode
    # Sending the mode a custom order already reads as leaves the order alone.
    custom = ["grade", "client", "staff"]
    ok_data(await put(client, owner, org, custom))
    assert ok_data(await client.get(url, headers=owner.headers))["organization"]["rate_mode"] == (
        "staff_first"
    )
    patched = ok_data(
        await client.patch(url, headers=owner.headers, json={"rate_mode": "staff_first"})
    )
    assert patched["wip_valuation_sources"] == custom


# --- Preview and save --------------------------------------------------------------------


async def revalue_world(client: AsyncClient):
    """Under the staff-first order: one unbilled entry at the staff rate (4 600) and one each
    in draft, billed, written off and in a locked period, all on the person's rate."""
    owner, org = await practice(client)
    person = await staff(org)
    await set_grade(org, person, "SNR")
    acme = await make_client(org)
    await new_rate(
        client, owner, org, kind="charge", user_id=person.id, rate=4_600, effective_from=FROM
    )
    await set_grade_rates(org, "SNR", charge=3_700)
    ok_data(await put(client, owner, org, STAFF_FIRST))
    entries = {}
    for day, status in ((7, "unbilled"), (8, "in_draft"), (9, "billed"), (10, "written_off")):
        entries[status] = await make_time_entry(
            org,
            person,
            client=acme,
            entry_date=date(2026, 9, day),
            charge_rate=4_600,
            cost_rate=0,
            rate_source="staff",
        )
        if status != "unbilled":
            await bill(entries[status].id, status)
    entries["locked"] = await make_time_entry(
        org,
        person,
        client=acme,
        entry_date=date(2026, 8, 3),
        charge_rate=4_600,
        cost_rate=0,
        rate_source="staff",
    )
    return owner, org, person, entries


@pytest.fixture
def august_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    async def locked(session, organization_id, days):
        return {d: "Period closed on 2026-08-31" for d in days if d.month == 8}

    monkeypatch.setattr(period_locks, "locked_dates", locked)


async def test_the_preview_says_what_re_valuing_would_do_and_saves_nothing(
    client: AsyncClient, august_locked: None
) -> None:
    owner, org, _person, entries = await revalue_world(client)
    shown = ok_data(await preview(client, owner, org, NEW_PRACTICE))
    assert shown == {
        "entries_changed": 1,
        "wip_before": 4_600,
        "wip_after": 3_700,
        "skipped": {"billed": 1, "in_draft": 1, "locked": 1, "written_off": 1},
        "too_many": False,
    }
    # The order it already has changes nothing.
    assert ok_data(await preview(client, owner, org, STAFF_FIRST))["entries_changed"] == 0
    found = ok_data(await client.get(f"{API}/organizations/{org['id']}", headers=owner.headers))
    assert found["organization"]["wip_valuation_sources"] == STAFF_FIRST
    assert (await entry_row(entries["unbilled"].id)).charge_rate == 4_600


async def test_saving_with_revalue_re_values_unbilled_unlocked_time_only(
    client: AsyncClient, august_locked: None
) -> None:
    owner, org, _person, entries = await revalue_world(client)
    response = await put(client, owner, org, NEW_PRACTICE, revalue=True)
    assert ok_data(response)["wip_valuation_sources"] == NEW_PRACTICE
    assert response.json()["message"] == "WIP valuation saved, 1 entries re-valued"
    row = await entry_row(entries["unbilled"].id)
    assert (row.charge_rate, row.charge_value, row.rate_source) == (3_700, 3_700, "grade")
    for status in ("in_draft", "billed", "written_off", "locked"):
        row = await entry_row(entries[status].id)
        assert (row.charge_rate, row.rate_source) == (4_600, "staff"), status

    updated = (await audit_entries(org["id"], "organization.updated"))[-1]
    assert updated["metadata"]["changes"] == {"wip_valuation_sources": [STAFF_FIRST, NEW_PRACTICE]}
    [rerun] = await audit_entries(org["id"], "rate.rerun")
    meta = rerun["metadata"]
    assert meta["wip_valuation_sources"] == NEW_PRACTICE
    assert (meta["entries_changed"], meta["entries_skipped"]) == (1, 4)
    assert (meta["charge_value_before"], meta["charge_value_after"]) == (4_600, 3_700)


async def test_saving_without_revalue_leaves_recorded_time(client: AsyncClient) -> None:
    owner, org, person, entries = await revalue_world(client)
    before = len(await audit_entries(org["id"], "organization.updated"))
    response = await put(client, owner, org, NEW_PRACTICE)
    assert response.json()["message"] == "WIP valuation saved"
    assert (await entry_row(entries["unbilled"].id)).charge_rate == 4_600
    assert await audit_entries(org["id"], "rate.rerun") == []
    assert len(await audit_entries(org["id"], "organization.updated")) == before + 1
    # New time is priced by the new order.
    entry = await log_time(client, person, org, entry_date="2026-10-05")
    assert (entry["charge_rate"], entry["rate_source"]) == (3_700, "grade")
    # Saving the same order again records nothing.
    ok_data(await put(client, owner, org, NEW_PRACTICE))
    assert len(await audit_entries(org["id"], "organization.updated")) == before + 1


async def test_bad_orders_and_callers_are_refused(client: AsyncClient) -> None:
    owner, org = await practice(client)
    for sources in ([], ["client", "client"], ["role"], ["client", "service", "grade", "x"]):
        for response in (
            await put(client, owner, org, sources),
            await preview(client, owner, org, sources),
        ):
            error = err(response, 422)
            assert error["code"] == "WIP_VALUATION_SOURCES", sources
            assert error["details"][0]["field"] == "sources"
    assert err(await put(client, owner, org, []), 422)["message"] == (
        "Keep at least one rate switched on."
    )
    # The database refuses an empty order or a repeated source as well.
    for bad in ("'{}'", "'{client,client}'", "'{client,role}'"):
        with pytest.raises(IntegrityError, match="ck_organizations_wip_valuation_sources"):
            await sql(
                f"UPDATE organizations SET wip_valuation_sources = {bad}::text[] WHERE id = :o",  # noqa: S608
                o=org["id"],
            )
    # Editing the practice settings is needed.
    person = await staff(org)
    assert err(await put(client, person, org, NEW_PRACTICE), 403)
    assert err(await preview(client, person, org, NEW_PRACTICE), 403)


async def test_re_valuing_above_the_cap_is_refused(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, org, _person, entries = await revalue_world(client)
    monkeypatch.setattr(wip_valuation, "MAX_ENTRIES", 0)
    shown = ok_data(await preview(client, owner, org, NEW_PRACTICE))
    assert shown == {
        "entries_changed": None,
        "wip_before": None,
        "wip_after": None,
        "skipped": None,
        "too_many": True,
    }
    error = err(await put(client, owner, org, NEW_PRACTICE, revalue=True), 422)
    assert error["code"] == "WIP_VALUATION_TOO_LARGE"
    assert "Change Rates on Past Time" in error["message"]
    found = ok_data(await client.get(f"{API}/organizations/{org['id']}", headers=owner.headers))
    assert found["organization"]["wip_valuation_sources"] == STAFF_FIRST
    # Without re-valuing it saves.
    ok_data(await put(client, owner, org, NEW_PRACTICE))
    assert (await entry_row(entries["unbilled"].id)).charge_rate == 4_600


# --- Other screens -----------------------------------------------------------------------


async def test_the_client_jobs_tab_follows_the_order(client: AsyncClient) -> None:
    owner, org, _person, acme, _svc, job = await world(client)

    async def row() -> dict:
        found = ok_data(
            await client.get(org_url(org, f"/clients/{acme.id}/job-rates"), headers=owner.headers)
        )
        return next(r for r in found["jobs"] if r["job_id"] == str(job.id))

    assert (await row())["source"] == "client_rule"
    ok_data(await put(client, owner, org, ["service", "client", "staff"]))
    assert (await row())["source"] == "task"
    ok_data(await put(client, owner, org, ["staff", "client"]))
    shown = await row()
    assert (shown["source"], shown["task_rate"]) == ("staff", None)
    ok_data(await put(client, owner, org, ["grade", "service"]))
    shown = await row()
    assert (shown["source"], shown["client_rule"]) == ("staff", None)
    await sql("UPDATE jobs SET hourly_rate = 6400 WHERE id = :j", j=str(job.id))
    await sql("DELETE FROM rate_rules WHERE level = 'client'")
    ok_data(await put(client, owner, org, ["client", "service"]))
    assert (await row())["source"] == "job"


async def test_re_rating_counts_a_new_source_as_a_change(client: AsyncClient) -> None:
    """Same rate, another source: the source is brought up to date. Time without a recorded
    source changes only when its rates do."""
    owner, org = await practice(client)
    person = await staff(org)
    await set_grade(org, person, "SNR")
    acme = await make_client(org)
    await new_rate(
        client, owner, org, kind="charge", user_id=person.id, rate=3_700, effective_from=FROM
    )
    await set_grade_rates(org, "SNR", charge=3_700)
    graded = await make_time_entry(
        org, person, client=acme, charge_rate=3_700, cost_rate=0, rate_source="grade"
    )
    unknown = await make_time_entry(org, person, client=acme, charge_rate=3_700, cost_rate=0)
    ok_data(await put(client, owner, org, STAFF_FIRST))
    scope = {"client_ids": [str(acme.id)]}
    shown = ok_data(
        await client.post(
            org_url(org, "/rates/rerate/preview"), headers=owner.headers, json={"scope": scope}
        )
    )
    assert shown["totals"]["entries_to_change"] == 1
    [item] = shown["items"]
    assert item["entry_id"] == str(graded.id)
    assert (item["old"]["source"], item["new"]["source"]) == ("grade", "staff")
    assert item["old"]["charge_rate"] == item["new"]["charge_rate"] == 3_700
    applied = await client.post(
        org_url(org, "/rates/rerate"),
        headers=owner.headers,
        json={"scope": scope, "preview_token": shown["preview_token"]},
    )
    assert applied.status_code == 201, applied.text
    assert (await entry_row(graded.id)).rate_source == "staff"
    assert (await entry_row(unknown.id)).rate_source is None


async def test_the_routes_are_documented(client: AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    base = "/api/v1/organizations/{organization_id}"
    for path, method, codes in (
        ("/wip-valuation/preview", "post", ["WIP_VALUATION_SOURCES"]),
        ("/wip-valuation", "put", ["WIP_VALUATION_SOURCES", "WIP_VALUATION_TOO_LARGE"]),
    ):
        operation = spec["paths"][base + path][method]
        assert operation["summary"] and "`organization.update`" in operation["description"]
        for code in codes:
            assert f"`{code}`" in operation["description"], (path, code)
        assert {"401", "403", "404", "422"} <= set(operation["responses"])
    schemas = spec["components"]["schemas"]
    for name in ("WipValuationPreview", "WipValuationSkipped", "WipValuationUpdate"):
        for field, prop in schemas[name]["properties"].items():
            assert prop.get("description") or any(
                o.get("description") for o in prop.get("anyOf", [])
            ), (name, field)
    read = schemas["OrganizationRead"]["properties"]
    assert set(read["wip_valuation_sources"]["items"]["enum"]) == set(NEW_PRACTICE)
    assert "Deprecated" in read["rate_mode"]["description"]


# --- Migration 080 -----------------------------------------------------------------------


async def test_migration_080_gives_each_practice_the_order_of_its_rate_mode(
    client: AsyncClient,
) -> None:
    practices = {}
    for mode in ("staff_first", "grade_only", "staff_only"):
        owner, org = await practice(client, f"Practice {mode}")
        ok_data(
            await client.patch(
                f"{API}/organizations/{org['id']}", headers=owner.headers, json={"rate_mode": mode}
            )
        )
        practices[mode] = (owner, org)
    owner, org = practices["staff_first"]
    custom_owner, custom = await practice(client, "Custom Order")
    ok_data(await put(client, custom_owner, custom, ["grade", "client"]))
    person = await staff(org)
    no_rate = await make_time_entry(org, person, charge_rate=0, cost_rate=0, rate_source="none")
    # Every write of the order keeps the former rate mode column in step (one release).
    rows = await sql("SELECT id::text AS id, rate_mode FROM organizations")
    modes = {row["id"]: row["rate_mode"] for row in rows}
    for mode, (_, practice_org) in practices.items():
        assert modes[practice_org["id"]] == mode
    assert modes[custom["id"]] == "grade_only"

    try:
        down = await migrate("downgrade", "079")
        assert down.returncode == 0, down.stderr
        rows = await sql("SELECT id::text AS id, rate_mode FROM organizations")
        modes = {row["id"]: row["rate_mode"] for row in rows}
        for mode, (_, practice_org) in practices.items():
            assert modes[practice_org["id"]] == mode
        assert modes[custom["id"]] == "grade_only"
        assert await sql(
            "SELECT rate_source FROM time_entries WHERE id = :i", i=str(no_rate.id)
        ) == [{"rate_source": "default"}]
        # The time entries refuse "none" again.
        with pytest.raises(IntegrityError, match="ck_time_entries_rate_source"):
            await sql(
                "UPDATE time_entries SET rate_source = 'none' WHERE id = :i", i=str(no_rate.id)
            )
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert await sql("SELECT version_num FROM alembic_version") == [
        {"version_num": head_revision()}
    ]
    rows = await sql("SELECT id::text AS id, wip_valuation_sources AS s FROM organizations")
    sources = {row["id"]: row["s"] for row in rows}
    for mode, (_, practice_org) in practices.items():
        assert sources[practice_org["id"]] == list(RATE_MODE_SOURCES[mode]), mode
    assert sources[custom["id"]] == list(RATE_MODE_SOURCES["grade_only"])
    # The former column stays for one release, in step with the order.
    rows = await sql("SELECT id::text AS id, rate_mode FROM organizations")
    modes = {row["id"]: row["rate_mode"] for row in rows}
    for mode, (_, practice_org) in practices.items():
        assert modes[practice_org["id"]] == mode
    ok_data(await put(client, custom_owner, custom, ["client", "staff"]))
    assert await sql("SELECT rate_mode FROM organizations WHERE id = :o", o=custom["id"]) == [
        {"rate_mode": "staff_only"}
    ]
    # A practice created now starts with the ticket's default order.
    _, fresh = await practice(client, "Fresh Practice")
    assert fresh["wip_valuation_sources"] == NEW_PRACTICE
    check = await migrate("check")
    assert check.returncode == 0, check.stdout + check.stderr

"""Overtime: the practice switch and the multiplier of migration 061 (the practice's and a staff
member's own). APT81 retired the multiplier: it is still saved, but overtime never changes the
charge-out or cost rate of time (it is costed apart, see test_overtime_capture.py), and
``is_overtime`` in a request is ignored (it is worked out from the working hours)."""

from datetime import date

import pytest
from httpx import AsyncClient, Response

from app.utils.money import time_value
from tests.domain_factories import make_client, make_job, make_staff_rate, practice, staff
from tests.factories import API, audit_entries
from tests.time_helpers import (
    entry_row,
    log_time,
    ok_data,
    org_url,
    set_entry,
    set_practice,
    time_job,
)


async def patch_org(client: AsyncClient, actor, org: dict, **body) -> Response:
    return await client.patch(f"{API}/organizations/{org['id']}", headers=actor.headers, json=body)


async def patch_staff(client: AsyncClient, actor, org: dict, person, **body) -> Response:
    return await client.patch(org_url(org, f"/staff/{person.id}"), headers=actor.headers, json=body)


async def scene(client: AsyncClient, *, enabled: bool = True):
    """A practice, a person on 60.00 charge / 10.00 cost an hour and a client."""
    owner, org = await practice(client)
    await set_practice(org, time_increment_minutes=1, overtime_enabled=enabled)
    person = await staff(org)
    await make_staff_rate(org, person, 6_000, 1_000)
    acme = await make_client(org)
    return owner, org, person, acme


# --- The practice setting ----------------------------------------------------------------------


async def test_overtime_is_off_by_default_at_one_and_a_half(client):
    owner, org = await practice(client)
    assert org["overtime_enabled"] is False and org["overtime_multiplier_bp"] == 15_000
    fresh = ok_data(await client.get(f"{API}/organizations/{org['id']}", headers=owner.headers))
    assert fresh["organization"]["overtime_enabled"] is False


async def test_the_switch_and_the_multiplier_are_saved_and_audited(client):
    owner, org = await practice(client)
    response = await patch_org(
        client, owner, org, overtime_enabled=True, overtime_multiplier_bp=20_000
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["overtime_enabled"] is True and data["overtime_multiplier_bp"] == 20_000
    [entry] = await audit_entries(org["id"], "organization.updated")
    assert entry["metadata"]["changes"] == {
        "overtime_enabled": [False, True],
        "overtime_multiplier_bp": [15_000, 20_000],
    }


@pytest.mark.parametrize(
    "body",
    [
        {"overtime_multiplier_bp": 9_999},
        {"overtime_multiplier_bp": 100_001},
        {"overtime_multiplier_bp": None},
        {"overtime_multiplier_bp": 1.5},
        {"overtime_enabled": None},
    ],
)
async def test_the_overtime_settings_are_validated(client, body):
    owner, org = await practice(client)
    response = await patch_org(client, owner, org, **body)
    assert response.status_code == 422, response.text


async def test_only_those_who_may_update_the_practice_change_it(client):
    _, org = await practice(client)
    member = await staff(org)
    assert (await patch_org(client, member, org, overtime_enabled=True)).status_code == 403


# --- Pricing: the multiplier is retired -------------------------------------------------------


async def test_overtime_never_changes_the_charge_or_cost_rate(client):
    _, org, person, acme = await scene(client)
    normal = await log_time(client, person, org, client_id=acme.id, minutes=90)
    asked = await log_time(client, person, org, client_id=acme.id, minutes=90, is_overtime=True)
    # The flag sent is ignored: Sam has no working hours, so nothing is overtime.
    assert normal["is_overtime"] is False and asked["is_overtime"] is False
    assert (normal["charge_rate"], normal["charge_value"]) == (6_000, 9_000)
    assert (asked["charge_rate"], asked["charge_value"]) == (6_000, 9_000)
    row = await entry_row(asked["id"])
    assert (row.cost_rate, row.cost_value, row.is_overtime) == (1_000, 1_500, False)
    assert row.rate_source == "staff"
    created = await audit_entries(org["id"], "time_entry.created")
    assert all("is_overtime" not in e["metadata"] for e in created)


async def test_odd_rates_and_a_job_rate_are_not_multiplied(client):
    _, org, _, acme = await scene(client)
    odd = await staff(org)
    await make_staff_rate(org, odd, 3_333, 1_001)
    entry = await log_time(client, odd, org, client_id=acme.id, minutes=60, is_overtime=True)
    assert entry["charge_rate"] == 3_333
    assert (await entry_row(entry["id"])).cost_rate == 1_001
    assert entry["charge_value"] == time_value(60, 3_333)
    job = await make_job(org, acme, hourly_rate=10_000)
    on_job = await log_time(client, odd, org, job_id=job.id, minutes=60, is_overtime=True)
    assert on_job["charge_rate"] == 10_000 and on_job["rate_source"] == "job"


async def test_a_staff_members_own_multiplier_is_saved_but_never_applied(client):
    owner, org, person, acme = await scene(client)
    response = await patch_staff(client, owner, org, person, overtime_multiplier_bp=20_000)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["overtime_multiplier_bp"] == 20_000
    own = await log_time(client, person, org, client_id=acme.id, is_overtime=True)
    assert own["charge_rate"] == 6_000
    cleared = await patch_staff(client, owner, org, person, overtime_multiplier_bp=None)
    assert cleared.status_code == 200 and cleared.json()["data"]["overtime_multiplier_bp"] is None


@pytest.mark.parametrize("value", [9_999, 100_001, "x"])
async def test_a_staff_multiplier_is_validated(client, value):
    owner, org, person, _ = await scene(client)
    response = await patch_staff(client, owner, org, person, overtime_multiplier_bp=value)
    assert response.status_code == 422, response.text


async def test_a_staff_member_has_no_multiplier_until_one_is_set(client):
    owner, org, person, _ = await scene(client)
    detail = ok_data(await client.get(org_url(org, f"/staff/{person.id}"), headers=owner.headers))
    assert detail["overtime_multiplier_bp"] is None
    changed = await audit_entries(org["id"], "staff.updated")
    assert changed == []
    await patch_staff(client, owner, org, person, overtime_multiplier_bp=18_000)
    [entry] = await audit_entries(org["id"], "staff.updated")
    assert entry["metadata"]["changes"] == {"overtime_multiplier_bp": [None, 18_000]}


# --- Editing -----------------------------------------------------------------------------------


async def test_is_overtime_in_an_edit_is_ignored(client):
    _, org, person, acme = await scene(client)
    entry = await log_time(client, person, org, client_id=acme.id, minutes=60)
    url = org_url(org, f"/time-entries/{entry['id']}")
    marked = ok_data(await client.patch(url, headers=person.headers, json={"is_overtime": True}))
    assert (marked["is_overtime"], marked["charge_rate"], marked["charge_value"]) == (
        False,
        6_000,
        6_000,
    )
    longer = ok_data(await client.patch(url, headers=person.headers, json={"minutes": 120}))
    assert (longer["is_overtime"], longer["charge_value"]) == (False, 12_000)
    updated = await audit_entries(org["id"], "time_entry.updated")
    assert all("is_overtime" not in e["metadata"]["changes"] for e in updated)


async def test_is_overtime_cannot_be_null_on_an_edit(client):
    _, org, person, acme = await scene(client)
    entry = await log_time(client, person, org, client_id=acme.id)
    response = await client.patch(
        org_url(org, f"/time-entries/{entry['id']}"),
        headers=person.headers,
        json={"is_overtime": None},
    )
    assert response.status_code == 422, response.text


async def test_rerating_prices_time_recorded_at_the_multiplier_at_the_normal_rates(client):
    owner, org, person, acme = await scene(client)
    entry = await log_time(client, person, org, client_id=acme.id, minutes=60)
    plain = await log_time(client, person, org, client_id=acme.id, minutes=60)
    # Time recorded before APT81 at the multiplier.
    await set_entry(entry["id"], is_overtime=True, charge_rate=9_000, cost_rate=1_500)
    await make_staff_rate(org, person, 8_000, 2_000, date(2026, 9, 1))
    scope = {"date_from": "2026-09-01", "date_to": "2026-09-30"}
    previewed = ok_data(
        await client.post(
            org_url(org, "/rates/rerate/preview"), headers=owner.headers, json={"scope": scope}
        )
    )
    applied = await client.post(
        org_url(org, "/rates/rerate"),
        headers=owner.headers,
        json={"scope": scope, "preview_token": previewed["preview_token"]},
    )
    assert applied.status_code == 201, applied.text
    row, normal = await entry_row(entry["id"]), await entry_row(plain["id"])
    assert (row.charge_rate, row.cost_rate) == (8_000, 2_000)
    assert (normal.charge_rate, normal.cost_rate) == (8_000, 2_000)


# --- Switched off ------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/time-entries", "/time-entries/bulk"])
async def test_is_overtime_is_accepted_and_ignored_while_it_is_off(client, path):
    _, org, person, acme = await scene(client, enabled=False)
    item = {
        "entry_date": "2026-09-07",
        "minutes": 60,
        "client_id": str(acme.id),
        "job_id": str(await time_job(org, acme.id)),
        "is_overtime": True,
    }
    body = {"entries": [item]} if path.endswith("bulk") else item
    response = await client.post(org_url(org, path), headers=person.headers, json=body)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert (data[0] if path.endswith("bulk") else data)["is_overtime"] is False


async def test_with_overtime_off_time_is_priced_exactly_as_before(client):
    _, org, person, acme = await scene(client, enabled=False)
    entry = await log_time(client, person, org, client_id=acme.id, minutes=45)
    assert entry["is_overtime"] is False
    assert (entry["charge_rate"], entry["charge_value"]) == (6_000, 4_500)
    row = await entry_row(entry["id"])
    assert (row.cost_rate, row.cost_value) == (1_000, 750)
    explicit = await log_time(client, person, org, client_id=acme.id, minutes=45, is_overtime=False)
    assert explicit["charge_value"] == 4_500


async def test_values_recorded_at_the_multiplier_stay_until_the_entry_is_repriced(client):
    owner, org, person, acme = await scene(client)
    entry = await log_time(client, person, org, client_id=acme.id, minutes=60)
    await set_entry(entry["id"], is_overtime=True, charge_rate=9_000, charge_value=9_000)
    assert (await patch_org(client, owner, org, overtime_enabled=False)).status_code == 200
    # What was recorded stays as it was ...
    assert (await entry_row(entry["id"])).charge_rate == 9_000
    # ... and a repriced entry goes back to the normal rate.
    edited = ok_data(
        await client.patch(
            org_url(org, f"/time-entries/{entry['id']}"),
            headers=person.headers,
            json={"minutes": 30},
        )
    )
    assert edited["charge_rate"] == 6_000

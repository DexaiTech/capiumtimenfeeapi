"""Backdated rate changes (APT66): ``POST /rates/impact`` says what adding a rate between the
existing ones would do, and ``POST /rates`` with ``confirm_impact`` does it in one transaction:
the rules are arranged, the unbilled time of the new period is re-priced (billed and
period-locked time is skipped) and the rate history and audit log record every step."""

from datetime import date

import pytest
from httpx import AsyncClient

from app.services import period_locks
from tests.clients_helpers import sql
from tests.domain_factories import make_client, make_time_entry, practice, staff
from tests.factories import audit_entries
from tests.team_helpers import bill, new_rate, rate_error, rule_count
from tests.time_helpers import entry_row, ok_data, org_url


async def impact(client: AsyncClient, actor, org: dict, **body) -> dict:
    response = await client.post(org_url(org, "/rates/impact"), headers=actor.headers, json=body)
    return ok_data(response)


async def rule(client: AsyncClient, actor, org: dict, rule_id: str) -> dict:
    return ok_data(await client.get(org_url(org, f"/rates/{rule_id}"), headers=actor.headers))


async def scene(client: AsyncClient):
    """Ada's cost was £40 from 01/04/2025 until a £60 rate started on 26/10/2026 (open-ended).
    Her charge-out rate is £100 throughout. Time recorded at £40 cost:

    * march   15/03/2026  unbilled  (before the new rate: stays at £40)
    * may     05/05/2026  unbilled  (re-priced to £50)
    * june    01/06/2026  billed    (skipped)
    * july    01/07/2026  unbilled, in a locked period (skipped)
    * october 27/10/2026  unbilled at £60 (the later rate's: untouched)
    """
    owner, org = await practice(client)
    ada = await staff(org)
    acme = await make_client(org)
    await new_rate(
        client, owner, org, kind="charge", user_id=ada.id, rate=10_000, effective_from="2025-01-01"
    )
    earlier = await new_rate(
        client, owner, org, kind="cost", user_id=ada.id, rate=4_000, effective_from="2025-04-01"
    )
    later = await new_rate(
        client,
        owner,
        org,
        kind="cost",
        user_id=ada.id,
        rate=6_000,
        effective_from="2026-10-26",
        close_previous=True,
    )

    async def entry(day: date, cost: int = 4_000):
        return await make_time_entry(
            org, ada, client=acme, entry_date=day, minutes=60, charge_rate=10_000, cost_rate=cost
        )

    entries = {
        "march": await entry(date(2026, 3, 15)),
        "may": await entry(date(2026, 5, 5)),
        "june": await entry(date(2026, 6, 1)),
        "july": await entry(date(2026, 7, 1)),
        "october": await entry(date(2026, 10, 27), cost=6_000),
    }
    await bill(entries["june"].id)
    return owner, org, ada, earlier, later, entries


def lock_july(monkeypatch: pytest.MonkeyPatch) -> None:
    async def locked(session, organization_id, days):
        return {d: "Period closed on 2026-07-31" for d in days if d.month == 7}

    monkeypatch.setattr(period_locks, "locked_dates", locked)


async def test_a_backdated_rate_before_a_later_one_is_refused_without_confirmation(
    client: AsyncClient,
) -> None:
    owner, org, ada, earlier, later, entries = await scene(client)
    body = {
        "kind": "cost",
        "user_id": str(ada.id),
        "rate": 5_000,
        "effective_from": "2026-04-01",
        "close_previous": True,
    }
    error = await rate_error(client, owner, org, 409, **body)
    assert error["code"] == "RATE_OVERLAP"
    assert {c["id"] for c in error["details"]["conflicts"]} == {earlier["id"], later["id"]}
    assert await rule_count(org, kind="cost") == 2
    assert (await rule(client, owner, org, earlier["id"]))["effective_to"] == "2026-10-25"
    assert (await entry_row(entries["may"].id)).cost_rate == 4_000


async def test_impact_describes_the_change_and_changes_nothing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, org, ada, earlier, later, entries = await scene(client)
    lock_july(monkeypatch)
    audits_before = len(await audit_entries(org["id"]))

    shown = await impact(
        client,
        owner,
        org,
        kind="cost",
        user_id=str(ada.id),
        rate=5_000,
        effective_from="2026-04-01",
        close_previous=True,
    )

    assert (shown["effective_from"], shown["effective_to"]) == ("2026-04-01", "2026-10-25")
    assert shown["ends"] == {
        "id": earlier["id"],
        "rate": 4_000,
        "effective_from": "2025-04-01",
        "effective_to": "2026-10-25",
        "new_effective_to": "2026-03-31",
    }
    assert shown["stops_at"] == {
        "id": later["id"],
        "rate": 6_000,
        "effective_from": "2026-10-26",
        "effective_to": None,
        "new_effective_to": None,
    }
    totals = shown["reprice"]
    assert (totals["entries_examined"], totals["entries_to_change"]) == (1, 1)
    assert (totals["charge_value_delta"], totals["cost_value_delta"]) == (0, 1_000)
    assert shown["skipped"] == {"billed": 1, "in_draft": 0, "written_off": 0, "period_locked": 1}
    assert shown["needs_confirmation"] is True

    # Nothing was written: rules, time, history and audit are as they were.
    assert await rule_count(org, kind="cost") == 2
    assert (await rule(client, owner, org, earlier["id"]))["effective_to"] == "2026-10-25"
    assert (await entry_row(entries["may"].id)).cost_rate == 4_000
    assert len(await audit_entries(org["id"])) == audits_before
    assert not await sql("SELECT 1 FROM rate_reruns WHERE organization_id = :o", o=org["id"])


async def test_confirmed_backdated_rate_is_placed_and_reprices_unbilled_time(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, org, ada, earlier, later, entries = await scene(client)
    lock_july(monkeypatch)

    response = await client.post(
        org_url(org, "/rates"),
        headers=owner.headers,
        json={
            "kind": "cost",
            "user_id": str(ada.id),
            "rate": 5_000,
            "effective_from": "2026-04-01",
            "close_previous": True,
            "confirm_impact": True,
        },
    )
    created = ok_data(response, 201)
    assert response.json()["message"] == "Rate added, 1 entries re-priced"

    # The rules: £40 to 31/03/2026, £50 to 25/10/2026, £60 from 26/10/2026 (kept as it was).
    assert (created["rate"], created["effective_from"], created["effective_to"]) == (
        5_000,
        "2026-04-01",
        "2026-10-25",
    )
    assert (await rule(client, owner, org, earlier["id"]))["effective_to"] == "2026-03-31"
    kept = await rule(client, owner, org, later["id"])
    assert (kept["rate"], kept["effective_from"], kept["effective_to"]) == (
        6_000,
        "2026-10-26",
        None,
    )

    # Only the unbilled, unlocked time of the new period is re-priced.
    may = await entry_row(entries["may"].id)
    assert (may.cost_rate, may.cost_value, may.charge_rate) == (5_000, 5_000, 10_000)
    for name, cost in (("march", 4_000), ("june", 4_000), ("july", 4_000), ("october", 6_000)):
        assert (await entry_row(entries[name].id)).cost_rate == cost, name

    # The re-rate run reports what it changed and skipped.
    [run] = ok_data(await client.get(org_url(org, "/rates/reruns"), headers=owner.headers))
    assert (run["source"], run["entries_changed"], run["entries_skipped"]) == ("rerate", 1, 2)
    assert run["note"] == "Re-priced for the new cost rate from 01/04/2026"
    assert run["scope"]["user_ids"] == [str(ada.id)]
    assert (run["scope"]["date_from"], run["scope"]["date_to"]) == ("2026-04-01", "2026-10-25")
    items = ok_data(
        await client.get(org_url(org, f"/rates/reruns/{run['id']}/items"), headers=owner.headers)
    )
    assert {(i["entry_id"], i["outcome"]) for i in items} == {
        (str(entries["may"].id), "changed"),
        (str(entries["july"].id), "skipped"),
    }

    # Rate history: the new rule created, the earlier one end-dated.
    rows = await sql(
        'SELECT rule_id, action, "new" AS after FROM rate_changes '
        "WHERE organization_id = :o AND rule_id IN (:new, :old)",
        o=org["id"],
        new=created["id"],
        old=earlier["id"],
    )
    history = {
        (str(r["rule_id"]), r["action"], (r["after"] or {}).get("effective_to")) for r in rows
    }
    assert (earlier["id"], "end_dated", "2026-03-31") in history
    assert (created["id"], "created", "2026-10-25") in history

    # Audit: the rule added, the earlier rule's end date and the re-rate.
    [added] = [
        a for a in await audit_entries(org["id"], "rate.created") if a["target_id"] == created["id"]
    ]
    assert added["metadata"]["effective_to"] == "2026-10-25"
    ended = [
        a["metadata"]["changes"]
        for a in await audit_entries(org["id"], "rate.updated")
        if a["target_id"] == earlier["id"]
    ]
    assert ended[-1] == {"effective_to": ["2026-10-25", "2026-03-31"]}
    [rerun] = await audit_entries(org["id"], "rate.rerun")
    assert rerun["target_type"] == "rate_rerun" and rerun["target_id"] == run["id"]
    assert rerun["metadata"]["rate_rule_id"] == created["id"]
    assert (rerun["metadata"]["entries_changed"], rerun["metadata"]["entries_skipped"]) == (1, 2)


async def test_a_plain_forward_rate_needs_no_confirmation_and_a_same_day_rate_is_refused(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    ada = await staff(org)
    current = await new_rate(
        client, owner, org, kind="charge", user_id=ada.id, rate=9_000, effective_from="2026-01-01"
    )
    body = {"kind": "charge", "user_id": str(ada.id), "rate": 9_500, "effective_from": "2027-01-01"}

    # "End the current rate the day before" ticked: nothing to ask about.
    shown = await impact(client, owner, org, **body, close_previous=True)
    assert (
        shown["ends"]["id"] == current["id"] and shown["ends"]["new_effective_to"] == "2026-12-31"
    )
    assert shown["stops_at"] is None and shown["effective_to"] is None
    assert shown["reprice"]["entries_to_change"] == 0
    assert shown["needs_confirmation"] is False
    # Not ticked: ending the running rate is asked about first.
    assert (await impact(client, owner, org, **body))["needs_confirmation"] is True

    # A rate that starts the day another one does cannot be placed.
    response = await client.post(
        org_url(org, "/rates/impact"),
        headers=owner.headers,
        json={**body, "effective_from": "2026-01-01"},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "RATE_OVERLAP"
    assert await rule_count(org) == 1

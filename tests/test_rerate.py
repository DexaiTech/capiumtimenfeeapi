"""Re-rating unbilled time: preview, apply (only what was previewed), runs and items, skips,
the V1 ``/rates/revalue`` and cost masking."""

import asyncio
from datetime import date, timedelta

import pytest
from httpx import AsyncClient

from app.services import period_locks, rerate
from tests.clients_helpers import scalar, sql
from tests.domain_factories import (
    make_client,
    make_staff_rate,
    make_time_entry,
    practice,
    staff,
)
from tests.factories import add_member, audit_entries
from tests.foundation_helpers import add_custom_role
from tests.team_helpers import bill
from tests.time_helpers import entry_row, make_client_rate, ok_data, org_url

SEPTEMBER = {"date_from": "2026-09-01", "date_to": "2026-09-30"}


async def preview(client: AsyncClient, actor, org: dict, scope: dict, **extra) -> dict:
    response = await client.post(
        org_url(org, "/rates/rerate/preview"),
        headers=actor.headers,
        json={"scope": scope, **extra},
    )
    return ok_data(response)


async def apply(client: AsyncClient, actor, org: dict, scope: dict, token: str, **extra):
    return await client.post(
        org_url(org, "/rates/rerate"),
        headers=actor.headers,
        json={"scope": scope, "preview_token": token, **extra},
    )


async def scene(client: AsyncClient):
    """Entries at yesterday's rates and rates that have since changed.

    * e1  person  acme  60 min  6 000 / 1 000  ->  client rate 12 000, cost 2 000
    * e2  person  beta  30 min  6 000 / 1 000  ->  staff rate 9 000, cost 2 000
    * e3  other   acme  60 min  6 000 / 1 000  ->  client rate 12 000, cost stays 1 000
    * e4  person  beta  60 min  9 000 / 2 000  ->  nothing changes
    """
    owner, org = await practice(client)
    person, other = await staff(org), await staff(org)
    acme, beta = await make_client(org), await make_client(org)
    await make_staff_rate(org, person, 6_000, 1_000)
    await make_staff_rate(org, other, 6_000, 1_000)
    e1 = await make_time_entry(
        org,
        person,
        client=acme,
        entry_date=date(2026, 9, 7),
        minutes=60,
        charge_rate=6_000,
        cost_rate=1_000,
    )
    e2 = await make_time_entry(
        org,
        person,
        client=beta,
        entry_date=date(2026, 9, 8),
        minutes=30,
        charge_rate=6_000,
        cost_rate=1_000,
    )
    e3 = await make_time_entry(
        org,
        other,
        client=acme,
        entry_date=date(2026, 9, 8),
        minutes=60,
        charge_rate=6_000,
        cost_rate=1_000,
    )
    e4 = await make_time_entry(
        org,
        person,
        client=beta,
        entry_date=date(2026, 9, 10),
        minutes=60,
        charge_rate=9_000,
        cost_rate=2_000,
    )
    await make_staff_rate(org, person, 9_000, 2_000, date(2026, 9, 1))
    await make_client_rate(org, acme.id, 12_000)
    scope = {"user_ids": [str(person.id)], **SEPTEMBER}
    return owner, org, person, other, acme, beta, (e1, e2, e3, e4), scope


# --- Preview and apply -------------------------------------------------------------------------


async def test_preview_changes_nothing_and_apply_does_exactly_what_was_previewed(
    client: AsyncClient,
) -> None:
    owner, org, person, _, _, _, (e1, e2, e3, e4), scope = await scene(client)
    shown = await preview(client, owner, org, scope, include_unchanged=True)

    assert shown["totals"] == {
        "entries_examined": 3,
        "entries_to_change": 2,
        "entries_unchanged": 1,
        "entries_skipped": 0,
        "charge_value_before": 6_000 + 3_000 + 9_000,
        "charge_value_after": 12_000 + 4_500 + 9_000,
        "charge_value_delta": 7_500,
        "cost_value_before": 1_000 + 500 + 2_000,
        "cost_value_after": 2_000 + 1_000 + 2_000,
        "cost_value_delta": 1_500,
    }
    assert shown["skipped"] == {"billed": 0, "in_draft": 0, "written_off": 0, "period_locked": 0}
    assert shown["scope"]["user_ids"] == [str(person.id)] and shown["items_truncated"] is False
    by_id = {item["entry_id"]: item for item in shown["items"]}
    assert set(by_id) == {str(e1.id), str(e2.id), str(e4.id)}
    first = by_id[str(e1.id)]
    assert (
        first["outcome"] == "change"
        and first["minutes"] == 60
        and first["entry_date"] == "2026-09-07"
    )
    assert (first["old"]["charge_rate"], first["old"]["charge_value"]) == (6_000, 6_000)
    assert (first["new"]["charge_rate"], first["new"]["charge_value"], first["new"]["source"]) == (
        12_000,
        12_000,
        "client",
    )
    assert (first["new"]["cost_rate"], first["new"]["cost_value"]) == (2_000, 2_000)
    assert first["staff"]["id"] == str(person.id) and first["client"]["name"]
    assert by_id[str(e2.id)]["new"]["source"] == "staff"
    assert by_id[str(e4.id)]["outcome"] == "unchanged"
    # Nothing was written.
    assert (await entry_row(e1.id)).charge_rate == 6_000
    assert await scalar("SELECT count(*) FROM rate_reruns") == 0

    response = await apply(client, owner, org, scope, shown["preview_token"], note="Autumn review")
    run = ok_data(response, 201)
    assert (run["entries_examined"], run["entries_changed"], run["entries_skipped"]) == (3, 2, 0)
    assert run["charge_value_before"] == 18_000 and run["charge_value_after"] == 25_500
    assert run["charge_value_delta"] == shown["totals"]["charge_value_delta"]
    assert run["cost_value_delta"] == shown["totals"]["cost_value_delta"]
    assert run["source"] == "rerate" and run["note"] == "Autumn review"
    assert run["requested_by"] == str(owner.id) and run["requested_by_name"] == owner.full_name

    after = {e.id: await entry_row(e.id) for e in (e1, e2, e3, e4)}
    assert (after[e1.id].charge_rate, after[e1.id].charge_value, after[e1.id].rate_source) == (
        12_000,
        12_000,
        "client",
    )
    assert (after[e1.id].cost_rate, after[e1.id].cost_value) == (2_000, 2_000)
    assert (after[e2.id].charge_rate, after[e2.id].charge_value, after[e2.id].rate_source) == (
        9_000,
        4_500,
        "staff",
    )
    assert after[e3.id].charge_rate == 6_000  # not in scope (another person)
    assert after[e4.id].rate_source is None  # unchanged entries are not touched

    # The same scope again has nothing left to change.
    again = await preview(client, owner, org, scope)
    assert again["totals"]["entries_to_change"] == 0 and again["items"] == []

    # The run, its items and the audit row agree with the preview.
    items = ok_data(
        await client.get(org_url(org, f"/rates/reruns/{run['id']}/items"), headers=owner.headers)
    )
    assert {i["entry_id"] for i in items} == {str(e1.id), str(e2.id)}
    first_item = next(i for i in items if i["entry_id"] == str(e1.id))
    assert (first_item["old"]["charge_rate"], first_item["new"]["charge_rate"]) == (6_000, 12_000)
    assert (first_item["old"]["source"], first_item["new"]["source"]) == (None, "client")
    assert first_item["outcome"] == "changed" and first_item["skip_reason"] is None
    [log] = await audit_entries(org["id"], "rate.rerun")
    assert log["target_type"] == "rate_rerun" and log["target_id"] == run["id"]
    assert log["metadata"]["entries_changed"] == 2 and log["metadata"]["note"] == "Autumn review"
    assert log["metadata"]["charge_value_after"] == 25_500


async def test_apply_needs_a_valid_fresh_preview_of_the_same_scope(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, org, person, other, acme, _beta, (_e1, _e2, _e3, _e4), scope = await scene(client)
    partner = await staff(org, "partner")
    token = (await preview(client, owner, org, scope))["preview_token"]

    def code(response) -> str:
        assert response.status_code == 409, response.text
        return response.json()["error"]["code"]

    # Missing, garbage, tampered, someone else's, another practice's.
    assert code(await apply(client, owner, org, scope, "not-a-token")) == "PREVIEW_REQUIRED"
    assert code(await apply(client, owner, org, scope, token[:-3] + "abc")) == "PREVIEW_REQUIRED"
    assert code(await apply(client, partner, org, scope, token)) == "PREVIEW_REQUIRED"
    _, other_org = await practice(client, "Other & Co")
    await add_member(other_org["id"], "owner", user=owner)
    assert code(await apply(client, owner, other_org, scope, token)) == "PREVIEW_REQUIRED"
    # Another scope than the one previewed.
    narrower = {**scope, "client_ids": [str(acme.id)]}
    assert code(await apply(client, owner, org, narrower, token)) == "PREVIEW_MISMATCH"
    # The order of ids in the scope does not matter.
    wide = {"user_ids": [str(person.id), str(other.id)], **SEPTEMBER}
    wide_token = (await preview(client, owner, org, wide))["preview_token"]
    swapped = {"user_ids": [str(other.id), str(person.id)], **SEPTEMBER}
    assert (await apply(client, owner, org, swapped, wide_token)).status_code == 201

    # Anything that changed the result since the preview makes it stale.
    owner2, org2, _person2, _, _acme2, _, (f1, f2, *_), scope2 = await scene(client)
    token2 = (await preview(client, owner2, org2, scope2))["preview_token"]
    await bill(f1.id)
    stale = await apply(client, owner2, org2, scope2, token2)
    assert code(stale) == "PREVIEW_STALE"
    assert stale.json()["error"]["details"]["entries_to_change"] == 1
    assert (await entry_row(f2.id)).charge_rate == 6_000
    # A fresh preview works.
    fresh = (await preview(client, owner2, org2, scope2))["preview_token"]
    assert (await apply(client, owner2, org2, scope2, fresh)).status_code == 201

    # Expired.
    monkeypatch.setattr(rerate, "TOKEN_TTL", timedelta(seconds=-1))
    owner3, org3, _, _, _, _, _, scope3 = await scene(client)
    expired = (await preview(client, owner3, org3, scope3))["preview_token"]
    assert code(await apply(client, owner3, org3, scope3, expired)) == "PREVIEW_REQUIRED"


async def test_only_unbilled_entries_change(client: AsyncClient) -> None:
    owner, org, person, _, acme, _beta, (e1, e2, _e3, _e4), scope = await scene(client)
    extra = [
        await make_time_entry(
            org,
            person,
            client=acme,
            entry_date=date(2026, 9, 9 + i),
            minutes=60,
            charge_rate=6_000,
            cost_rate=1_000,
        )
        for i in range(3)
    ]
    for entry, status in zip(extra, ("in_draft", "billed", "written_off"), strict=True):
        await bill(entry.id, status)

    shown = await preview(client, owner, org, scope)
    assert shown["skipped"] == {"billed": 1, "in_draft": 1, "written_off": 1, "period_locked": 0}
    assert shown["totals"]["entries_skipped"] == 3
    assert shown["totals"]["entries_examined"] == 3  # e1, e2, e4: the unbilled ones
    assert {i["entry_id"] for i in shown["items"]} == {str(e1.id), str(e2.id)}
    run = ok_data(await apply(client, owner, org, scope, shown["preview_token"]), 201)
    assert (run["entries_changed"], run["entries_skipped"]) == (2, 3)
    for entry in extra:
        assert (await entry_row(entry.id)).charge_rate == 6_000
    assert (await entry_row(e1.id)).charge_rate == 12_000
    # Skipped because of their billing status is a count; nothing per entry is stored.
    items = ok_data(
        await client.get(org_url(org, f"/rates/reruns/{run['id']}/items"), headers=owner.headers)
    )
    assert len(items) == 2


async def test_entries_in_locked_periods_are_skipped_with_a_reason(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, org, _person, _, _, _, (e1, e2, _e3, _e4), scope = await scene(client)

    async def locked(session, organization_id, days):
        return {d: "Period closed on 2026-09-30" for d in days if d == date(2026, 9, 7)}

    monkeypatch.setattr(period_locks, "locked_dates", locked)
    shown = await preview(client, owner, org, scope)
    assert shown["skipped"]["period_locked"] == 1
    assert shown["totals"]["entries_examined"] == 2 and shown["totals"]["entries_to_change"] == 1
    skipped = next(i for i in shown["items"] if i["outcome"] == "skipped")
    assert (
        skipped["entry_id"] == str(e1.id)
        and skipped["skip_reason"] == "Period closed on 2026-09-30"
    )
    assert (skipped["old"]["charge_rate"], skipped["new"]["charge_rate"]) == (6_000, 6_000)

    run = ok_data(await apply(client, owner, org, scope, shown["preview_token"]), 201)
    assert (run["entries_changed"], run["entries_skipped"]) == (1, 1)
    assert (await entry_row(e1.id)).charge_rate == 6_000  # untouched
    assert (await entry_row(e2.id)).charge_rate == 9_000
    items = ok_data(
        await client.get(org_url(org, f"/rates/reruns/{run['id']}/items"), headers=owner.headers)
    )
    assert {(i["entry_id"], i["outcome"]) for i in items} == {
        (str(e1.id), "skipped"),
        (str(e2.id), "changed"),
    }
    only_skipped = ok_data(
        await client.get(
            org_url(org, f"/rates/reruns/{run['id']}/items"),
            headers=owner.headers,
            params={"outcome": "skipped"},
        )
    )
    assert [i["skip_reason"] for i in only_skipped] == ["Period closed on 2026-09-30"]
    assert (
        ok_data(
            await client.get(org_url(org, f"/rates/reruns/{run['id']}"), headers=owner.headers)
        )["entries_skipped"]
        == 1
    )


async def test_scope_filters_narrow_each_other(client: AsyncClient) -> None:
    owner, org, _person, other, acme, beta, (e1, e2, e3, _e4), _scope = await scene(client)

    async def ids(scope_: dict) -> set[str]:
        shown = await preview(client, owner, org, scope_)
        return {i["entry_id"] for i in shown["items"]}

    assert await ids(SEPTEMBER) == {str(e1.id), str(e2.id), str(e3.id)}
    assert await ids({"client_ids": [str(acme.id)]}) == {str(e1.id), str(e3.id)}
    assert await ids({"client_ids": [str(acme.id)], "user_ids": [str(other.id)]}) == {str(e3.id)}
    assert await ids({**SEPTEMBER, "date_from": "2026-09-08"}) == {str(e2.id), str(e3.id)}
    assert (
        await ids(
            {"client_ids": [str(beta.id)], "date_from": "2026-09-09", "date_to": "2026-09-30"}
        )
        == set()
    )
    # Another practice's client matches nothing.
    _, other_org = await practice(client, "Other & Co")
    theirs = await make_client(other_org)
    assert await ids({"client_ids": [str(theirs.id)]}) == set()


async def test_scope_validation_and_size_limit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, org, person, _, acme, _, _, _scope = await scene(client)
    url = org_url(org, "/rates/rerate/preview")
    for bad in (
        {},
        {"user_ids": [str(person.id)]},
        {"date_from": "2026-09-01"},
        {"date_from": "2026-09-30", "date_to": "2026-09-01"},
        {"client_ids": ["not-a-uuid"]},
    ):
        response = await client.post(url, headers=owner.headers, json={"scope": bad})
        assert response.status_code == 422, bad
    assert (await client.post(url, headers=owner.headers, json={})).status_code == 422
    # Clients alone, or jobs alone, or a date range alone is enough.
    for good in ({"client_ids": [str(acme.id)]}, SEPTEMBER, {"job_ids": [str(person.id)]}):
        assert (
            await client.post(url, headers=owner.headers, json={"scope": good})
        ).status_code == 200

    monkeypatch.setattr(rerate, "MAX_ENTRIES", 2)
    response = await client.post(url, headers=owner.headers, json={"scope": SEPTEMBER})
    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["code"] == "scope_too_large"
    assert await scalar("SELECT count(*) FROM rate_reruns") == 0


async def test_item_limit_truncates_the_list_but_not_the_totals(client: AsyncClient) -> None:
    owner, org, _, _, _, _, (_e1, _e2, _e3, _e4), _scope = await scene(client)
    full = await preview(client, owner, org, SEPTEMBER)
    short = await preview(client, owner, org, SEPTEMBER, item_limit=1)
    assert short["totals"] == full["totals"]
    assert len(short["items"]) == 1 and short["items_truncated"] is True
    assert short["items"][0]["entry_date"] == "2026-09-07"  # by date
    none = await preview(client, owner, org, SEPTEMBER, item_limit=0)
    assert none["items"] == [] and none["items_truncated"] is True
    with_unchanged = await preview(client, owner, org, SEPTEMBER, include_unchanged=True)
    assert len(with_unchanged["items"]) == 4
    too_many = await client.post(
        org_url(org, "/rates/rerate/preview"),
        headers=owner.headers,
        json={"scope": SEPTEMBER, "item_limit": 5001},
    )
    assert too_many.status_code == 422


async def test_two_applications_of_one_preview_cannot_both_succeed(client: AsyncClient) -> None:
    owner, org, _, _, _, _, _, scope = await scene(client)
    token = (await preview(client, owner, org, scope))["preview_token"]
    responses = await asyncio.gather(
        apply(client, owner, org, scope, token), apply(client, owner, org, scope, token)
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert await scalar("SELECT count(*) FROM rate_reruns") == 1


# --- Runs ------------------------------------------------------------------------------------------


async def test_runs_are_listed_newest_first_and_isolated_per_practice(client: AsyncClient) -> None:
    owner, org, _person, _, _acme, _, _, scope = await scene(client)
    first = ok_data(
        await apply(
            client, owner, org, scope, (await preview(client, owner, org, scope))["preview_token"]
        ),
        201,
    )
    wider = {**SEPTEMBER}
    second = ok_data(
        await apply(
            client, owner, org, wider, (await preview(client, owner, org, wider))["preview_token"]
        ),
        201,
    )
    runs = ok_data(await client.get(org_url(org, "/rates/reruns"), headers=owner.headers))
    assert [r["id"] for r in runs] == [second["id"], first["id"]]
    paged = await client.get(
        org_url(org, "/rates/reruns"), headers=owner.headers, params={"limit": 1}
    )
    assert len(paged.json()["data"]) == 1 and paged.json()["meta"]["next_cursor"]

    outsider_owner, theirs = await practice(client, "Other & Co")
    for path in (f"/rates/reruns/{first['id']}", f"/rates/reruns/{first['id']}/items"):
        assert (
            await client.get(org_url(theirs, path), headers=outsider_owner.headers)
        ).status_code == 404
    assert (
        ok_data(await client.get(org_url(theirs, "/rates/reruns"), headers=outsider_owner.headers))
        == []
    )
    # Not a member of the practice at all: 404 for the whole area.
    assert (
        await client.get(
            org_url(org, f"/rates/reruns/{first['id']}"), headers=outsider_owner.headers
        )
    ).status_code == 404


async def test_the_v1_revalue_is_applied_straight_away_and_recorded_as_a_run(
    client: AsyncClient,
) -> None:
    owner, org, person, _other, _acme, _beta, (e1, _e2, _e3, _e4), _scope = await scene(client)
    result = ok_data(
        await client.post(
            org_url(org, "/rates/revalue"),
            headers=owner.headers,
            json={**SEPTEMBER, "user_id": str(person.id)},
        )
    )
    assert (result["entries_examined"], result["entries_changed"]) == (3, 2)
    assert result["value_delta"] == 7_500 and result["cost_value_delta"] == 1_500
    assert (await entry_row(e1.id)).rate_source == "client"
    [run] = ok_data(await client.get(org_url(org, "/rates/reruns"), headers=owner.headers))
    assert run["source"] == "revalue" and run["entries_changed"] == 2
    assert (
        run["scope"]["user_ids"] == [str(person.id)] and run["scope"]["date_from"] == "2026-09-01"
    )
    [log] = await audit_entries(org["id"], "time_entries.revalued")
    assert log["metadata"]["entries_changed"] == 2 and log["metadata"]["user_id"] == str(person.id)
    assert log["metadata"]["run_id"] == run["id"]


async def test_cost_values_are_masked_without_rate_read(client: AsyncClient) -> None:
    owner, org, _person, _, _, _, (e1, *_), scope = await scene(client)
    await add_custom_role(org["id"], "Rate editor", ["member.read", "rate.manage"])
    editor = await add_member(org["id"], "Rate editor")
    shown = await preview(client, editor, org, scope, include_unchanged=True)
    totals = shown["totals"]
    assert (
        totals["cost_value_before"] is None
        and totals["cost_value_after"] is None
        and totals["cost_value_delta"] is None
    )
    assert totals["charge_value_delta"] == 7_500
    assert all(
        i["old"]["cost_rate"] is None and i["new"]["cost_value"] is None for i in shown["items"]
    )
    run = ok_data(await apply(client, editor, org, scope, shown["preview_token"]), 201)
    assert run["cost_value_before"] is None and run["cost_value_delta"] is None
    assert run["charge_value_delta"] == 7_500
    # ... though the cost was applied.
    assert (await entry_row(e1.id)).cost_rate == 2_000
    # The editor cannot list runs (rate.read), the owner sees the costs.
    assert (
        await client.get(org_url(org, "/rates/reruns"), headers=editor.headers)
    ).status_code == 403
    seen = ok_data(
        await client.get(org_url(org, f"/rates/reruns/{run['id']}"), headers=owner.headers)
    )
    assert seen["cost_value_delta"] == 1_500
    # The V1 call is masked the same way.
    result = ok_data(
        await client.post(org_url(org, "/rates/revalue"), headers=editor.headers, json=SEPTEMBER)
    )
    assert result["cost_value_delta"] is None


async def test_rerate_after_a_grade_default_change_uses_the_new_rate(client: AsyncClient) -> None:
    """The rate book is read at apply time: a change made after the entry was logged is
    picked up by re-rating, and the entry then records which level priced it."""
    owner, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    await sql(
        "UPDATE organization_members SET grade_id = (SELECT id FROM grades WHERE organization_id = :o AND code = 'MGR') "
        "WHERE organization_id = :o AND user_id = :u",
        o=org["id"],
        u=str(person.id),
    )
    entry = await make_time_entry(
        org,
        person,
        client=acme,
        entry_date=date(2026, 9, 7),
        minutes=60,
        charge_rate=100,
        cost_rate=0,
    )
    await sql(
        "UPDATE grades SET default_charge_rate = 7000, default_cost_rate = 2500 "
        "WHERE organization_id = :o AND code = 'MGR'",
        o=org["id"],
    )
    scope = {"client_ids": [str(acme.id)]}
    run = ok_data(
        await apply(
            client, owner, org, scope, (await preview(client, owner, org, scope))["preview_token"]
        ),
        201,
    )
    assert run["entries_changed"] == 1
    row = await entry_row(entry.id)
    assert (row.charge_rate, row.cost_rate, row.rate_source) == (7_000, 2_500, "grade")

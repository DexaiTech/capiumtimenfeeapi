"""Recurring invoice schedules: the template, pausing, and the daily job that drafts invoices."""

import uuid
from datetime import date, timedelta

from sqlalchemy import text

from app.db.session import get_sessionmaker
from app.services.recurring import first_after, first_on_or_after, occurrence, run_due
from tests.billing_helpers import count_rows, get_invoice, new_practice, org_url
from tests.domain_factories import vat_rate_id
from tests.factories import audit_entries, error

LINE = {"description": "Monthly bookkeeping", "unit_amount": 25_000}


def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


async def create(client, p, **body):
    response = await client.post(
        org_url(p.org, "/recurring"),
        headers=p.owner.headers,
        json={
            "client_id": str(p.client.id),
            "name": "Bookkeeping retainer",
            "frequency": "monthly",
            "start_date": days_ago(1),
            "lines": [LINE],
            **body,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def post(client, p, path, **body):
    return await client.post(org_url(p.org, path), headers=p.owner.headers, json=body or None)


async def runs(client, p, schedule_id):
    response = await client.get(
        org_url(p.org, f"/recurring/{schedule_id}/runs"), headers=p.owner.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def run_job() -> dict:
    async with get_sessionmaker()() as session:
        return await run_due(session)


def test_dates_are_counted_from_the_start():
    jan31 = date(2026, 1, 31)
    assert [occurrence(jan31, "monthly", n) for n in range(4)] == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
    ]
    assert occurrence(date(2026, 3, 2), "weekly", 3) == date(2026, 3, 23)
    assert occurrence(date(2024, 2, 29), "annually", 1) == date(2025, 2, 28)
    assert first_after(jan31, "quarterly", date(2026, 5, 1)) == date(2026, 7, 31)
    assert first_after(jan31, "monthly", date(2026, 1, 31)) == date(2026, 2, 28)
    assert first_on_or_after(jan31, "monthly", date(2026, 2, 28)) == date(2026, 2, 28)
    assert first_on_or_after(jan31, "monthly", date(2025, 6, 1)) == jan31


async def test_create_reads_back_with_its_template(client):
    p = await new_practice(client)
    schedule = await create(client, p, start_date="2099-01-31", reference="RET-1")
    assert (schedule["status"], schedule["next_run_date"]) == ("active", "2099-01-31")
    assert schedule["net_estimate"] == 25_000 and schedule["failed_runs"] == 0
    assert [(row["description"], row["unit_amount"]) for row in schedule["lines"]] == [
        ("Monthly bookkeeping", 25_000)
    ]
    assert schedule["client"]["id"] == str(p.client.id)
    [entry] = await audit_entries(p.id, "recurring.created")
    assert entry["target_id"] == schedule["id"]


async def test_create_rules(client):
    p = await new_practice(client)
    url = org_url(p.org, "/recurring")
    base = {
        "client_id": str(p.client.id),
        "name": "X",
        "frequency": "monthly",
        "start_date": "2026-01-01",
        "lines": [LINE],
    }
    backwards = await client.post(
        url, headers=p.owner.headers, json={**base, "end_date": "2025-12-31"}
    )
    assert backwards.status_code == 422
    daily = await client.post(url, headers=p.owner.headers, json={**base, "frequency": "daily"})
    assert daily.status_code == 422
    no_lines = await client.post(url, headers=p.owner.headers, json={**base, "lines": []})
    assert no_lines.status_code == 422
    missing = await client.post(
        url, headers=p.owner.headers, json={**base, "client_id": str(uuid.uuid4())}
    )
    assert missing.status_code == 404


async def test_the_job_drafts_an_invoice_and_moves_the_schedule_on(client):
    p = await new_practice(client, vat_registered=True)
    standard = await vat_rate_id(p.org, "standard")
    schedule = await create(
        client,
        p,
        reference="RET-1",
        notes="Thanks",
        lines=[{**LINE, "vat_rate_id": str(standard)}],
    )
    assert (await run_job())["drafted"] >= 1
    [run] = await runs(client, p, schedule["id"])
    assert run["status"] == "succeeded" and run["run_date"] == days_ago(1)
    invoice = await get_invoice(client, p.owner, p.org, run["invoice_id"])
    assert invoice["status"] == "draft"
    assert (invoice["reference"], invoice["notes"]) == ("RET-1", "Thanks")
    assert (invoice["net_total"], invoice["vat_total"], invoice["gross_total"]) == (
        25_000,
        5_000,
        30_000,
    )
    after = (
        await client.get(org_url(p.org, f"/recurring/{schedule['id']}"), headers=p.owner.headers)
    ).json()["data"]
    assert after["last_run_on"] == days_ago(1)
    assert after["next_run_date"] > date.today().isoformat()
    [entry] = await audit_entries(p.id, "recurring.run")
    assert entry["metadata"]["invoice_id"] == run["invoice_id"]


async def test_the_job_is_idempotent(client):
    p = await new_practice(client)
    schedule = await create(client, p)
    await run_job()
    await run_job()
    assert len(await runs(client, p, schedule["id"])) == 1
    assert await count_rows("SELECT count(*) FROM invoices WHERE organization_id = :o", o=p.id) == 1


async def test_the_job_catches_up_a_few_missed_dates_but_not_a_flood(client):
    p = await new_practice(client)
    schedule = await create(client, p, frequency="weekly", start_date=days_ago(15))
    await run_job()
    dates = sorted(r["run_date"] for r in await runs(client, p, schedule["id"]))
    assert dates == [days_ago(15), days_ago(8), days_ago(1)]


async def test_the_job_ignores_paused_future_and_ended_schedules(client):
    p = await new_practice(client)
    future = await create(client, p, start_date="2099-01-01")
    paused = await create(client, p, name="Paused")
    assert (await post(client, p, f"/recurring/{paused['id']}/pause")).status_code == 200
    ended = await create(
        client, p, name="Ends", frequency="weekly", start_date=days_ago(1), end_date=days_ago(1)
    )
    await run_job()
    assert await runs(client, p, future["id"]) == []
    assert await runs(client, p, paused["id"]) == []
    [run] = await runs(client, p, ended["id"])
    assert run["status"] == "succeeded"
    after = (
        await client.get(org_url(p.org, f"/recurring/{ended['id']}"), headers=p.owner.headers)
    ).json()["data"]
    assert after["status"] == "ended" and after["next_run_date"] is None


async def test_pause_and_resume_skip_the_dates_missed(client):
    p = await new_practice(client)
    schedule = await create(client, p, frequency="weekly", start_date=days_ago(20))
    again = await post(client, p, f"/recurring/{schedule['id']}/resume")
    assert again.status_code == 409 and error(again)["code"] == "SCHEDULE_NOT_PAUSED"
    paused = (await post(client, p, f"/recurring/{schedule['id']}/pause")).json()["data"]
    assert paused["status"] == "paused"
    twice = await post(client, p, f"/recurring/{schedule['id']}/pause")
    assert twice.status_code == 409 and error(twice)["code"] == "SCHEDULE_NOT_ACTIVE"
    resumed = (await post(client, p, f"/recurring/{schedule['id']}/resume")).json()["data"]
    assert resumed["status"] == "active"
    assert resumed["next_run_date"] >= date.today().isoformat()
    await run_job()
    assert await runs(client, p, schedule["id"]) == [] or all(
        r["run_date"] >= date.today().isoformat() for r in await runs(client, p, schedule["id"])
    )


async def test_update_changes_the_template_for_the_next_run_only(client):
    p = await new_practice(client, vat_registered=False)
    schedule = await create(client, p, start_date="2099-03-31")
    url = org_url(p.org, f"/recurring/{schedule['id']}")
    response = await client.patch(
        url,
        headers=p.owner.headers,
        json={"name": "Renamed", "lines": [{"description": "Payroll", "unit_amount": 9_900}]},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["name"] == "Renamed" and data["net_estimate"] == 9_900
    quarterly = await client.patch(url, headers=p.owner.headers, json={"frequency": "quarterly"})
    assert quarterly.json()["data"]["next_run_date"] == "2099-03-31"
    nulled = await client.patch(url, headers=p.owner.headers, json={"lines": None})
    assert nulled.status_code == 422


async def test_an_end_date_before_the_next_run_ends_the_schedule(client):
    p = await new_practice(client)
    schedule = await create(client, p, start_date="2099-01-01")
    url = org_url(p.org, f"/recurring/{schedule['id']}")
    ended = await client.patch(url, headers=p.owner.headers, json={"end_date": "2099-01-01"})
    assert ended.json()["data"]["status"] == "active"  # it still runs on 1 January
    schedule2 = await create(client, p, name="Two", start_date="2099-02-01")
    moved = await client.patch(
        org_url(p.org, f"/recurring/{schedule2['id']}"),
        headers=p.owner.headers,
        json={"frequency": "weekly", "end_date": "2099-02-01"},
    )
    assert moved.status_code == 200
    after = await client.patch(
        org_url(p.org, f"/recurring/{schedule2['id']}"), headers=p.owner.headers, json={"name": "x"}
    )
    assert after.status_code == 200


async def test_delete_only_a_schedule_that_never_ran(client):
    p = await new_practice(client)
    fresh = await create(client, p, start_date="2099-01-01")
    used = await create(client, p, name="Used")
    await run_job()
    kept = await client.delete(org_url(p.org, f"/recurring/{used['id']}"), headers=p.owner.headers)
    assert kept.status_code == 409 and error(kept)["code"] == "SCHEDULE_HAS_RUNS"
    gone = await client.delete(org_url(p.org, f"/recurring/{fresh['id']}"), headers=p.owner.headers)
    assert gone.status_code == 204


async def set_rate_archived(rate_id, archived: bool) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            text("UPDATE vat_rates SET archived_at = CASE WHEN :on THEN now() END WHERE id = :id"),
            {"on": archived, "id": str(rate_id)},
        )
        await session.commit()


async def test_a_failed_run_waits_for_a_retry(client):
    p = await new_practice(client, vat_registered=True)
    standard = await vat_rate_id(p.org, "standard")
    schedule = await create(client, p, lines=[{**LINE, "vat_rate_id": str(standard)}])
    await set_rate_archived(standard, True)
    try:
        await run_job()
        await run_job()  # a failed date waits: the job does not try it again by itself
        [run] = await runs(client, p, schedule["id"])
        assert run["status"] == "failed" and run["invoice_id"] is None
        assert "VAT rate" in run["error"]
        shown = (
            await client.get(
                org_url(p.org, f"/recurring/{schedule['id']}"), headers=p.owner.headers
            )
        ).json()["data"]
        assert (shown["failed_runs"], shown["last_run_on"]) == (1, None)
        assert shown["next_run_date"] == days_ago(1)
        assert await audit_entries(p.id, "recurring.run_failed")

        still = (await post(client, p, f"/recurring/runs/{run['id']}/retry")).json()["data"]
        assert still["status"] == "failed" and still["attempts"] == 2
    finally:
        await set_rate_archived(standard, False)

    fixed = await post(client, p, f"/recurring/runs/{run['id']}/retry")
    assert fixed.status_code == 200, fixed.text
    done = fixed.json()["data"]
    assert (done["status"], done["attempts"], done["error"]) == ("succeeded", 3, None)
    invoice = await get_invoice(client, p.owner, p.org, done["invoice_id"])
    assert invoice["status"] == "draft" and invoice["gross_total"] == 30_000
    after = (
        await client.get(org_url(p.org, f"/recurring/{schedule['id']}"), headers=p.owner.headers)
    ).json()["data"]
    assert (after["failed_runs"], after["last_run_on"]) == (0, days_ago(1))
    assert after["next_run_date"] > date.today().isoformat()
    again = await post(client, p, f"/recurring/runs/{run['id']}/retry")
    assert again.status_code == 409 and error(again)["code"] == "RUN_NOT_FAILED"
    missing = await post(client, p, f"/recurring/runs/{uuid.uuid4()}/retry")
    assert missing.status_code == 404


async def test_list_filters_by_client_status_and_search(client):
    p = await new_practice(client)
    a = await create(client, p, name="Alpha retainer", start_date="2099-01-01")
    b = await create(client, p, name="Beta retainer", start_date="2099-01-01")
    await post(client, p, f"/recurring/{b['id']}/pause")

    async def ids(query):
        response = await client.get(org_url(p.org, f"/recurring{query}"), headers=p.owner.headers)
        assert response.status_code == 200, response.text
        return {s["id"] for s in response.json()["data"]}

    assert await ids("") == {a["id"], b["id"]}
    assert await ids("?status=paused") == {b["id"]}
    assert await ids("?status=active&status=paused") == {a["id"], b["id"]}
    assert await ids("?q=alpha") == {a["id"]}
    assert await ids(f"?client_id={p.client.id}") == {a["id"], b["id"]}

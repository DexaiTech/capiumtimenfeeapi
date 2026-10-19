"""Budget alerts at 80% and 100%: once per crossing, re-armed below the threshold, only while the
job is open, wired into every write that changes what a job has used."""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from app.db.models import OrganizationMember
from app.db.session import get_sessionmaker
from tests.domain_factories import make_client, make_time_entry, practice, staff
from tests.factories import Actor, audit_entries
from tests.job_helpers import (
    alert_keys,
    budget_notices,
    create_job,
    job_row,
    job_url,
    log_on_job,
    set_job_row,
)
from tests.time_gap_helpers import approved_entry, correct, day
from tests.time_helpers import MONDAY, age_timer, ok_data, org_url, set_entry


async def _setup(client: AsyncClient, assignees: tuple[str, ...] = ("ann", "bob"), **job_body):
    """A job of 600 minutes at £60 an hour (a minute is 100p) with a manager, a partner and the
    assignees named; one more member is on none of it."""
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    people = {
        "manager": await staff(org, "manager", full_name="Mia Manager"),
        "partner": await staff(org, "partner", full_name="Pat Partner"),
        "ann": await staff(org, full_name="Ann Able"),
        "bob": await staff(org, full_name="Bob Baker"),
        "carol": await staff(org, full_name="Carol Chance"),
    }
    body = {
        "budget_minutes": 600,
        "hourly_rate": 6000,
        "manager_user_id": people["manager"].id,
        "partner_user_id": people["partner"].id,
        "assignee_user_ids": [people[name].id for name in assignees],
        **job_body,
    }
    job = await create_job(client, owner, org, acme, "Accounts", **body)
    return owner, org, acme, job, people


async def _notices(client: AsyncClient, org: dict, people: dict[str, Actor]) -> dict[str, list]:
    return {name: await budget_notices(client, person, org) for name, person in people.items()}


def _percents(notices: list[dict]) -> list[int]:
    return [n["payload"]["percent"] for n in notices]


async def _delete_entry(client: AsyncClient, actor: Actor, org: dict, entry_id: str) -> None:
    response = await client.delete(org_url(org, f"/time-entries/{entry_id}"), headers=actor.headers)
    assert response.status_code == 200, response.text


async def test_80_then_100_each_alert_once(client: AsyncClient) -> None:
    owner, org, _, job, people = await _setup(client)
    await log_on_job(client, owner, org, job, 474)  # 79%
    assert await alert_keys(job["id"]) == []
    assert all(not found for found in (await _notices(client, org, people)).values())

    await log_on_job(client, owner, org, job, 6)  # 80%
    assert await alert_keys(job["id"]) == [("minutes", 80)]
    notices = await _notices(client, org, people)
    for name in ("manager", "partner", "ann", "bob"):
        assert len(notices[name]) == 1, name
    assert notices["carol"] == []
    notice = notices["manager"][0]
    assert notice["title"] == "Budget alert: Accounts"
    assert notice["body"] == "Accounts for Acme Ltd has used 80% of its hours budget."
    assert notice["link"] == f"/jobs/{job['id']}"
    assert notice["entity_type"] == "job" and notice["entity_id"] == job["id"]
    assert notice["payload"] == {"percent": 80, "kind": "minutes"}
    # The person who logged the time is told only when they are on the job.
    assert await budget_notices(client, owner, org) == []

    await log_on_job(client, owner, org, job, 60)  # 91%: no new alert
    assert await alert_keys(job["id"]) == [("minutes", 80)]
    assert len((await _notices(client, org, people))["manager"]) == 1

    await log_on_job(client, owner, org, job, 60)  # 100%
    assert await alert_keys(job["id"]) == [("minutes", 80), ("minutes", 100)]
    second = (await _notices(client, org, people))["manager"]
    assert _percents(second) == [80, 100]
    assert second[1]["body"] == "Accounts for Acme Ltd has used 100% of its hours budget."

    await log_on_job(client, owner, org, job, 12)  # 102%: nothing more
    assert await alert_keys(job["id"]) == [("minutes", 80), ("minutes", 100)]
    assert len((await _notices(client, org, people))["bob"]) == 2

    logged = await audit_entries(org["id"], "job.budget_alert")
    assert [(e["metadata"]["kind"], e["metadata"]["threshold"]) for e in logged] == [
        ("minutes", 80),
        ("minutes", 100),
    ]
    assert [e["metadata"]["usage_bp"] for e in logged] == [8000, 10_000]
    assert logged[0]["target_id"] == job["id"] and logged[0]["actor_user_id"] == str(owner.id)
    assert logged[0]["metadata"]["name"] == "Accounts"
    assert logged[0]["metadata"]["client_name"] == "Acme Ltd"


async def test_one_write_crossing_both_thresholds_notifies_once(client: AsyncClient) -> None:
    owner, org, _, job, people = await _setup(client)
    await log_on_job(client, owner, org, job, 660)  # 0 to 110% at once
    assert await alert_keys(job["id"]) == [("minutes", 80), ("minutes", 100)]
    # Both thresholds are recorded, but people are told once: about the highest.
    assert _percents((await _notices(client, org, people))["manager"]) == [100]
    assert len(await audit_entries(org["id"], "job.budget_alert")) == 2


async def test_dropping_below_a_threshold_re_arms_it(client: AsyncClient) -> None:
    owner, org, _, job, people = await _setup(client)
    big = await log_on_job(client, owner, org, job, 480)
    top = await log_on_job(client, owner, org, job, 120)
    assert await alert_keys(job["id"]) == [("minutes", 80), ("minutes", 100)]

    # Down to 80%: the 100% alert goes, the 80% one stays.
    await _delete_entry(client, owner, org, top["id"])
    assert await alert_keys(job["id"]) == [("minutes", 80)]
    # Up again: 100% is crossed again, and people are told again.
    await log_on_job(client, owner, org, job, 120, entry_date="2026-09-08")
    assert await alert_keys(job["id"]) == [("minutes", 80), ("minutes", 100)]
    assert _percents((await _notices(client, org, people))["ann"]) == [80, 100, 100]

    # Down to 20%: both alerts go, and crossing 80% is announced once more.
    await _delete_entry(client, owner, org, big["id"])
    assert await alert_keys(job["id"]) == []
    await log_on_job(client, owner, org, job, 360, entry_date="2026-09-09")
    assert await alert_keys(job["id"]) == [("minutes", 80)]
    assert _percents((await _notices(client, org, people))["ann"]) == [80, 100, 100, 80]


async def test_the_fee_budget_has_its_own_alerts(client: AsyncClient) -> None:
    owner, org, _, job, people = await _setup(client, budget_minutes=None, budget_fee=12_000)
    await log_on_job(client, owner, org, job, 90)  # £90 of £120: 75%
    await log_on_job(client, owner, org, job, 600, entry_date="2026-09-08", is_billable=False)
    assert await alert_keys(job["id"]) == []  # time that is not billable is not fee
    await log_on_job(client, owner, org, job, 6, entry_date="2026-09-09")  # £96: 80%
    assert await alert_keys(job["id"]) == [("fee", 80)]
    notice = (await _notices(client, org, people))["manager"][0]
    assert notice["body"] == "Accounts for Acme Ltd has used 80% of its fee budget."
    assert notice["payload"] == {"percent": 80, "kind": "fee"}
    await log_on_job(client, owner, org, job, 24, entry_date="2026-09-10")  # £120
    assert await alert_keys(job["id"]) == [("fee", 80), ("fee", 100)]


async def test_hours_and_fee_budgets_are_watched_separately(client: AsyncClient) -> None:
    owner, org, _, job, people = await _setup(client, budget_fee=12_000)
    await log_on_job(client, owner, org, job, 96)  # 16% of the hours, 80% of the fee
    assert await alert_keys(job["id"]) == [("fee", 80)]
    await log_on_job(client, owner, org, job, 384, entry_date="2026-09-08")  # 80% of the hours
    assert await alert_keys(job["id"]) == [("fee", 80), ("fee", 100), ("minutes", 80)]
    notices = (await _notices(client, org, people))["manager"]
    assert sorted((n["payload"]["kind"], n["payload"]["percent"]) for n in notices) == [
        ("fee", 80),
        ("fee", 100),
        ("minutes", 80),
    ]


async def test_only_open_jobs_raise_alerts(client: AsyncClient) -> None:
    owner, org, acme, job, people = await _setup(client)
    # Time put in directly, so that no write has looked at the budget: 90% used.
    row = await job_row(job["id"])
    await make_time_entry(org, owner, client=acme, job=row, minutes=540, charge_rate=6000)
    assert await alert_keys(job["id"]) == []
    evaluate = job_url(org, job["id"], "/budget/evaluate")
    for finished in ("completed", "closed", "cancelled"):
        await set_job_row(job["id"], status=finished)
        found = ok_data(await client.post(evaluate, headers=owner.headers))
        assert found["triggered"] == [] and found["alerts"] == [], finished
    await set_job_row(job["id"], status="on_hold")
    found = ok_data(await client.post(evaluate, headers=owner.headers))
    assert [(t["kind"], t["threshold"], t["usage_bp"]) for t in found["triggered"]] == [
        ("minutes", 80, 9000)
    ]
    assert _percents((await _notices(client, org, people))["manager"]) == [80]
    # A job that is finished still loses an alert it no longer deserves.
    await set_job_row(job["id"], status="completed")
    await set_job_row(job["id"], budget_minutes=1000)
    cleared = ok_data(await client.post(evaluate, headers=owner.headers))
    assert [(c["kind"], c["threshold"]) for c in cleared["cleared"]] == [("minutes", 80)]
    assert await alert_keys(job["id"]) == []


async def test_the_evaluate_endpoint(client: AsyncClient) -> None:
    owner, org, acme, job, people = await _setup(client)
    row = await job_row(job["id"])
    url = job_url(org, job["id"], "/budget/evaluate")
    quiet = ok_data(await client.post(url, headers=owner.headers))
    assert quiet == {
        "job_id": job["id"],
        "used_bp": 0,
        "rag": "green",
        "triggered": [],
        "cleared": [],
        "alerts": [],
    }
    entry = await make_time_entry(org, owner, client=acme, job=row, minutes=600, charge_rate=6000)
    found = ok_data(await client.post(url, headers=owner.headers))
    assert (found["used_bp"], found["rag"]) == (10_000, "red")
    assert [(t["kind"], t["threshold"]) for t in found["triggered"]] == [
        ("minutes", 80),
        ("minutes", 100),
    ]
    assert [(a["kind"], a["threshold"], a["usage_bp"]) for a in found["alerts"]] == [
        ("minutes", 80, 10_000),
        ("minutes", 100, 10_000),
    ]
    # Evaluating again changes nothing and tells nobody twice.
    again = ok_data(await client.post(url, headers=owner.headers))
    assert again["triggered"] == [] and again["cleared"] == [] and len(again["alerts"]) == 2
    assert len((await _notices(client, org, people))["manager"]) == 1
    # Less time than the thresholds: they are cleared.
    await set_entry(entry.id, minutes=120, charge_value=12_000)
    cleared = ok_data(await client.post(url, headers=owner.headers))
    assert [(c["kind"], c["threshold"]) for c in cleared["cleared"]] == [
        ("minutes", 80),
        ("minutes", 100),
    ]
    assert cleared["alerts"] == [] and cleared["used_bp"] == 2000

    # Who may: job.update; the job must exist in this practice.
    for role in ("staff", "viewer", "billing"):
        person = await staff(org, role)
        assert (await client.post(url, headers=person.headers)).status_code == 403, role
    manager = await staff(org, "manager")
    assert (await client.post(url, headers=manager.headers)).status_code == 200
    other_owner, _ = await practice(client, "Other & Co")
    assert (await client.post(url, headers=other_owner.headers)).status_code == 404
    unknown = job_url(org, uuid.uuid4(), "/budget/evaluate")
    assert (await client.post(unknown, headers=owner.headers)).status_code == 404
    assert (await client.post(url)).status_code == 401


async def test_changing_a_budget_re_evaluates(client: AsyncClient) -> None:
    owner, org, _, job, people = await _setup(client)
    await log_on_job(client, owner, org, job, 360)  # 60%
    url = job_url(org, job["id"])
    assert await alert_keys(job["id"]) == []
    # A smaller budget puts the job over 80%.
    data = ok_data(await client.patch(url, headers=owner.headers, json={"budget_minutes": 400}))
    assert data["budget_minutes_used_bp"] == 9000
    assert await alert_keys(job["id"]) == [("minutes", 80)]
    assert len((await _notices(client, org, people))["partner"]) == 1
    # A bigger one takes the alert away, so that it can be raised again.
    ok_data(await client.patch(url, headers=owner.headers, json={"budget_minutes": 1000}))
    assert await alert_keys(job["id"]) == []
    ok_data(await client.patch(url, headers=owner.headers, json={"budget_minutes": 400}))
    assert await alert_keys(job["id"]) == [("minutes", 80)]
    assert len((await _notices(client, org, people))["partner"]) == 2
    # Changing something else leaves the alerts alone.
    ok_data(await client.patch(url, headers=owner.headers, json={"name": "Renamed"}))
    assert await alert_keys(job["id"]) == [("minutes", 80)]
    # A fee budget too (the time is worth £360 at £60 an hour).
    ok_data(await client.patch(url, headers=owner.headers, json={"budget_fee": 40_000}))
    assert await alert_keys(job["id"]) == [("fee", 80), ("minutes", 80)]
    ok_data(await client.patch(url, headers=owner.headers, json={"budget_fee": None}))
    assert await alert_keys(job["id"]) == [("minutes", 80)]


async def test_entries_edits_deletes_timers_and_copies_are_covered(client: AsyncClient) -> None:
    owner, org, acme, job, _ = await _setup(client)
    other = await create_job(
        client, owner, org, acme, "Other", budget_minutes=600, hourly_rate=6000
    )

    # Bulk entries.
    response = await client.post(
        org_url(org, "/time-entries/bulk"),
        headers=owner.headers,
        json={
            "entries": [
                {
                    "entry_date": day(i),
                    "minutes": 120,
                    "job_id": job["id"],
                    "client_id": job["client_id"],
                }
                for i in range(4)
            ]
        },
    )
    assert response.status_code == 201, response.text
    assert await alert_keys(job["id"]) == [("minutes", 80)]  # 480 of 600

    # An edit that adds minutes, one that moves the time to another job, and one that adds more.
    entries = ok_data(await client.get(org_url(org, "/time-entries"), headers=owner.headers))
    url = org_url(org, f"/time-entries/{entries[0]['id']}")
    ok_data(await client.patch(url, headers=owner.headers, json={"minutes": 240}))  # 600
    assert await alert_keys(job["id"]) == [("minutes", 80), ("minutes", 100)]
    moved = {"job_id": other["id"], "minutes": 240}
    ok_data(await client.patch(url, headers=owner.headers, json=moved))
    assert await alert_keys(job["id"]) == []  # 360 of 600 left
    assert await alert_keys(other["id"]) == []  # 240 of 600
    ok_data(await client.patch(url, headers=owner.headers, json={"minutes": 480}))
    assert await alert_keys(other["id"]) == [("minutes", 80)]
    # An edit that changes nothing the budget is made of leaves the alerts alone.
    ok_data(await client.patch(url, headers=owner.headers, json={"description": "Notes"}))
    assert await alert_keys(other["id"]) == [("minutes", 80)]

    # A timer that is stopped.
    third = await create_job(
        client, owner, org, acme, "Third", budget_minutes=600, hourly_rate=6000
    )
    started = await client.post(
        org_url(org, "/timer/start"), headers=owner.headers, json={"job_id": third["id"]}
    )
    assert started.status_code == 201, started.text
    await age_timer(org, owner, 28_800)  # eight hours
    stopped = await client.post(org_url(org, "/timer/stop"), headers=owner.headers)
    assert stopped.status_code == 201, stopped.text
    assert await alert_keys(third["id"]) == [("minutes", 80)]

    # Copying a week of time.
    fourth = await create_job(
        client, owner, org, acme, "Fourth", budget_minutes=600, hourly_rate=6000
    )
    await log_on_job(client, owner, org, fourth, 300, entry_date=MONDAY.isoformat())
    copied = await client.post(
        org_url(org, "/time-entries/copy-week"),
        headers=owner.headers,
        json={"from_week_start": day(0), "to_week_start": day(7)},
    )
    assert copied.status_code == 200, copied.text
    assert len(copied.json()["data"]["created"]) == 1
    assert await alert_keys(fourth["id"]) == [("minutes", 80), ("minutes", 100)]


async def test_time_corrections_are_covered(client: AsyncClient) -> None:
    owner, org, acme, job, people = await _setup(client)
    approver = people["manager"]
    entry = await approved_entry(
        client,
        org,
        people["ann"],
        approver,
        job_id=job["id"],
        client_id=job["client_id"],
        minutes=60,
    )
    assert await alert_keys(job["id"]) == []
    await correct(client, approver, org, entry["id"], reason="Forgot the afternoon", minutes=540)
    assert await alert_keys(job["id"]) == [("minutes", 80)]
    await correct(client, approver, org, entry["id"], reason="Too much", minutes=60)
    assert await alert_keys(job["id"]) == []
    # Moving approved time to another job moves its usage with it.
    other = await create_job(client, owner, org, acme, "Other", budget_minutes=70, hourly_rate=6000)
    await correct(client, approver, org, entry["id"], reason="Wrong job", job_id=other["id"])
    assert await alert_keys(other["id"]) == [("minutes", 80)]
    assert await alert_keys(job["id"]) == []


async def test_revaluing_time_is_covered(client: AsyncClient) -> None:
    owner, org, _, job, _ = await _setup(
        client, budget_minutes=None, budget_fee=12_000, hourly_rate=3000
    )
    await log_on_job(client, owner, org, job, 96)  # £48 of £120: 40%
    assert await alert_keys(job["id"]) == []
    patch = {"hourly_rate": 6000}
    ok_data(await client.patch(job_url(org, job["id"]), headers=owner.headers, json=patch))
    revalued = await client.post(
        org_url(org, "/rates/revalue"),
        headers=owner.headers,
        json={"date_from": "2026-09-01", "date_to": "2026-09-30"},
    )
    assert revalued.status_code == 200, revalued.text
    assert await alert_keys(job["id"]) == [("fee", 80)]


async def test_an_alert_never_blocks_a_write(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, org, _, job, _ = await _setup(client)

    async def explode(*args, **kwargs):
        raise RuntimeError("alerts are down")

    monkeypatch.setattr("app.services.job_budget_alerts.evaluate_job_budget", explode)
    entry = await log_on_job(client, owner, org, job, 600)
    assert entry["minutes"] == 600
    url = org_url(org, f"/time-entries/{entry['id']}")
    assert (await client.patch(url, headers=owner.headers, json={"minutes": 12})).status_code == 200
    assert (await client.delete(url, headers=owner.headers)).status_code == 200
    assert await alert_keys(job["id"]) == []


async def test_a_failing_notification_does_not_block_the_write(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, org, _, job, _ = await _setup(client)

    async def explode(*args, **kwargs):
        raise RuntimeError("the notification service is down")

    monkeypatch.setattr("app.services.notifications.NotificationService.notify", explode)
    await log_on_job(client, owner, org, job, 600)
    # The alerts are recorded; only telling people failed.
    assert await alert_keys(job["id"]) == [("minutes", 80), ("minutes", 100)]


async def test_who_is_told(client: AsyncClient) -> None:
    owner, org, acme, job, people = await _setup(client, assignees=("manager", "ann", "bob"))
    # Bob left the practice: his assignment stays on the job, but nobody tells him anything.
    async with get_sessionmaker()() as session:
        await session.execute(
            update(OrganizationMember)
            .where(OrganizationMember.user_id == people["bob"].id)
            .values(status="removed")
        )
        await session.commit()
    await log_on_job(client, owner, org, job, 480)
    notices = await _notices(client, org, people)
    # The manager, who is also an assignee, is told once.
    assert {name: len(found) for name, found in notices.items()} == {
        "manager": 1,
        "partner": 1,
        "ann": 1,
        "bob": 0,
        "carol": 0,
    }

    # A job nobody is on still records its alerts.
    lonely = await create_job(
        client, owner, org, acme, "Lonely", budget_minutes=60, hourly_rate=6000
    )
    await log_on_job(client, owner, org, lonely, 60)
    assert await alert_keys(lonely["id"]) == [("minutes", 80), ("minutes", 100)]
    assert await budget_notices(client, owner, org) == []


async def test_the_alerts_in_force_are_in_the_budget_detail(client: AsyncClient) -> None:
    owner, org, _, job, _ = await _setup(client)
    await log_on_job(client, owner, org, job, 540)
    detail = ok_data(await client.get(job_url(org, job["id"], "/budget"), headers=owner.headers))
    assert [(a["kind"], a["threshold"], a["usage_bp"]) for a in detail["alerts"]] == [
        ("minutes", 80, 9000)
    ]
    assert detail["alerts"][0]["triggered_at"]


async def test_concurrent_writes_raise_one_alert(client: AsyncClient) -> None:
    owner, org, _, job, people = await _setup(client)
    await log_on_job(client, owner, org, job, 400)  # 66%
    # Neither write alone reaches 80% (460 of 600); together they do (520).
    await asyncio.gather(
        log_on_job(client, people["ann"], org, job, 60, entry_date="2026-09-08"),
        log_on_job(client, people["bob"], org, job, 60, entry_date="2026-09-08"),
    )
    assert await alert_keys(job["id"]) == [("minutes", 80)]
    assert len((await _notices(client, org, people))["manager"]) == 1
    assert len(await audit_entries(org["id"], "job.budget_alert")) == 1

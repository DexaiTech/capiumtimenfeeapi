"""Period locks: locking a week, month, quarter or year, unlocking one, the history, permissions,
isolation, the immutability of the history and the races between locking and writing."""

import asyncio
import uuid
from datetime import date, timedelta

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.exc import IntegrityError

from app.db.models import LockedPeriod
from app.db.session import get_sessionmaker
from app.repositories.period_locks import PeriodLockRepository
from app.services import period_locks
from tests.clients_helpers import scalar, sql
from tests.domain_factories import practice, staff
from tests.factories import audit_entries, error
from tests.foundation_helpers import add_custom_role
from tests.period_lock_helpers import (
    events,
    lock_period,
    locked_periods,
    org_url,
    set_lock,
    today,
    unlock_period,
)

DAY = timedelta(days=1)


def refused(response: Response, code: str, field: str) -> None:
    """A 422 whose error code and field detail both carry the business code."""
    assert response.status_code == 422, response.text
    body = error(response)
    assert body["code"] == code, body
    assert [(d["field"], d["code"]) for d in body["details"]] == [(field, code)], body


async def get_state(client: AsyncClient, actor, org: dict) -> dict:
    response = await client.get(org_url(org, "/period-locks"), headers=actor.headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def history(client: AsyncClient, actor, org: dict, **params) -> Response:
    return await client.get(
        org_url(org, "/period-locks/history"), headers=actor.headers, params=params
    )


def last_year(org: dict) -> int:
    return today(org).year - 1


# --- The state ---------------------------------------------------------------------------------


async def test_a_new_practice_has_nothing_locked(client):
    owner, org = await practice(client)
    state = await get_state(client, owner, org)
    assert state["periods"] == [] and state["last_event"] is None
    assert state["settings"] == {
        "granularity": "weekly",
        "financial_year_start_month": 1,
        "financial_year_start_day": 1,
        "configured": False,
    }
    assert (await history(client, owner, org)).json()["data"] == []


# --- Locking -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period_type", "period", "start", "end", "label"),
    [
        ("month", 2, (2, 1), (2, 28), "February"),
        ("quarter", 1, (1, 1), (3, 31), "Q1"),
        ("quarter", 4, (10, 1), (12, 31), "Q4"),
        ("year", None, (1, 1), (12, 31), ""),
    ],
)
async def test_locking_a_period_records_it_an_event_and_an_audit_entry(
    client, period_type, period, start, end, label
):
    owner, org = await practice(client)
    year = 2023  # not a leap year; long past
    response = await lock_period(client, owner, org, period_type, year, period, reason="Close")
    assert response.status_code == 200, response.text
    state = response.json()["data"]
    [locked] = state["periods"]
    assert locked["period_type"] == period_type
    assert locked["start_date"] == str(date(year, *start))
    assert locked["end_date"] == str(date(year, *end))
    assert locked["label"].startswith(label) and locked["label"].endswith(str(year))
    assert locked["reason"] == "Close" and locked["locked_by"] == str(owner.id)
    assert state["last_event"]["event_type"] == "locked"
    assert await locked_periods(org) == [
        {
            "period_type": period_type,
            "start_date": date(year, *start),
            "end_date": date(year, *end),
        }
    ]
    [event] = await events(org)
    assert event["event_type"] == "locked" and event["reason"] == "Close"
    assert event["actor_user_id"] == owner.id
    [entry] = await audit_entries(org["id"], "period_lock.locked")
    assert entry["metadata"]["period_start"] == str(date(year, *start))


async def test_periods_are_locked_independently(client):
    owner, org = await practice(client)
    y = last_year(org)
    assert (await lock_period(client, owner, org, "month", y, 1)).status_code == 200
    assert (await lock_period(client, owner, org, "month", y, 3)).status_code == 200
    state = await get_state(client, owner, org)
    assert [p["start_date"] for p in state["periods"]] == [str(date(y, 1, 1)), str(date(y, 3, 1))]
    # February, between them, stays open.
    async with get_sessionmaker()() as session:
        periods = await PeriodLockRepository(session).list_periods(uuid.UUID(org["id"]))
    assert not any(p.start_date <= date(y, 2, 10) <= p.end_date for p in periods)


async def test_a_period_must_have_ended(client):
    owner, org = await practice(client)
    t = today(org)
    refused(
        await lock_period(client, owner, org, "year", t.year), "PERIOD_LOCK_IN_FUTURE", "period"
    )
    refused(
        await lock_period(client, owner, org, "year", t.year + 1), "PERIOD_LOCK_IN_FUTURE", "period"
    )
    assert await locked_periods(org) == []


async def test_a_period_cannot_overlap_a_locked_one(client):
    owner, org = await practice(client)
    y = last_year(org)
    assert (await lock_period(client, owner, org, "quarter", y, 1)).status_code == 200
    refused(await lock_period(client, owner, org, "month", y, 2), "PERIOD_LOCK_OVERLAPS", "period")
    refused(await lock_period(client, owner, org, "year", y), "PERIOD_LOCK_OVERLAPS", "period")
    refused(
        await lock_period(client, owner, org, "quarter", y, 1), "PERIOD_LOCK_OVERLAPS", "period"
    )
    assert (await lock_period(client, owner, org, "quarter", y, 2)).status_code == 200
    assert len(await locked_periods(org)) == 2


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"year": 2023},
        {"period_type": "week", "year": 2023},
        {"period_type": "month", "year": 1999, "period": 1},
        {"period_type": "month", "year": 2023, "period": 13},
        {"period_type": "month", "year": 2023, "period": 0},
        {"period_type": "year", "year": 2023, "locked_through": "2023-12-31"},
    ],
)
async def test_lock_body_validation(client, body):
    owner, org = await practice(client)
    response = await client.post(org_url(org, "/period-locks"), headers=owner.headers, json=body)
    assert response.status_code == 422, response.text
    assert await locked_periods(org) == []


async def test_a_month_or_quarter_needs_its_number(client):
    owner, org = await practice(client)
    for period_type in ("month", "quarter"):
        refused(
            await lock_period(client, owner, org, period_type, 2023),
            "PERIOD_LOCK_INVALID_PERIOD",
            "period",
        )
    refused(
        await lock_period(client, owner, org, "quarter", 2023, 5),
        "PERIOD_LOCK_INVALID_PERIOD",
        "period",
    )


@pytest.mark.parametrize(("reason", "stored"), [(None, None), ("  ", None), (" Close ", " Close ")])
async def test_a_reason_is_optional_when_locking(client, reason, stored):
    owner, org = await practice(client)
    body = {} if reason is None else {"reason": reason}
    response = await lock_period(client, owner, org, "year", 2023, **body)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["periods"][0]["reason"] in (stored, stored and stored.strip())


# --- Unlocking ---------------------------------------------------------------------------------


async def test_unlocking_opens_only_that_period_and_records_why(client):
    owner, org = await practice(client)
    y = last_year(org)
    await lock_period(client, owner, org, "month", y, 1)
    await lock_period(client, owner, org, "month", y, 3)
    state = await get_state(client, owner, org)
    january = state["periods"][0]["id"]

    response = await unlock_period(client, owner, org, january, reason="A late invoice")
    assert response.status_code == 200, response.text
    [remaining] = response.json()["data"]["periods"]
    assert remaining["start_date"] == str(date(y, 3, 1))
    assert response.json()["data"]["last_event"]["event_type"] == "reopened"
    assert [e["event_type"] for e in await events(org)] == ["locked", "locked", "reopened"]
    last = (await events(org))[-1]
    assert last["period_start"] == date(y, 1, 1) and last["reason"] == "A late invoice"
    [entry] = await audit_entries(org["id"], "period_lock.reopened")
    assert entry["metadata"]["reason"] == "A late invoice"

    async with get_sessionmaker()() as session:
        org_id = uuid.UUID(org["id"])
        await period_locks.assert_open(session, org_id, date(y, 1, 15), "time")
        with pytest.raises(period_locks.PeriodLockedError):
            await period_locks.assert_open(session, org_id, date(y, 3, 15), "time")


@pytest.mark.parametrize("body", [{}, {"reason": None}, {"reason": ""}, {"reason": "   "}])
async def test_unlocking_needs_a_reason(client, body):
    owner, org = await practice(client)
    await lock_period(client, owner, org, "year", 2023)
    [period] = (await get_state(client, owner, org))["periods"]
    refused(
        await unlock_period(client, owner, org, period["id"], **body),
        "PERIOD_LOCK_REASON_REQUIRED",
        "reason",
    )
    assert len(await locked_periods(org)) == 1


async def test_unlocking_something_that_is_not_locked_is_a_404(client):
    owner, org = await practice(client)
    response = await unlock_period(client, owner, org, str(uuid.uuid4()), reason="Oops")
    assert response.status_code == 404, response.text
    await lock_period(client, owner, org, "year", 2023)
    [period] = (await get_state(client, owner, org))["periods"]
    assert (await unlock_period(client, owner, org, period["id"], reason="Once")).status_code == 200
    assert (
        await unlock_period(client, owner, org, period["id"], reason="Twice")
    ).status_code == 404


@pytest.mark.parametrize("body", [{}, {"reason": "Because"}, {"lock_id": "nope", "reason": "x"}])
async def test_unlock_body_validation_never_unlocks(client, body):
    owner, org = await practice(client)
    await lock_period(client, owner, org, "year", 2023)
    response = await client.post(
        org_url(org, "/period-locks/reopen"), headers=owner.headers, json=body
    )
    assert response.status_code == 422, response.text
    assert len(await locked_periods(org)) == 1


async def test_an_unlocked_period_can_be_locked_again(client):
    owner, org = await practice(client)
    await lock_period(client, owner, org, "year", 2023)
    [period] = (await get_state(client, owner, org))["periods"]
    await unlock_period(client, owner, org, period["id"], reason="Fix")
    assert (await lock_period(client, owner, org, "year", 2023)).status_code == 200
    assert [e["event_type"] for e in await events(org)] == ["locked", "reopened", "locked"]


async def test_a_legacy_lock_shows_and_can_be_unlocked(client):
    owner, org = await practice(client)
    await set_lock(org, date(2023, 8, 31))
    [legacy] = (await get_state(client, owner, org))["periods"]
    assert legacy["period_type"] == "legacy" and legacy["end_date"] == "2023-08-31"
    assert legacy["label"] == "Through 31/08/2023"
    assert (await unlock_period(client, owner, org, legacy["id"], reason="Why")).status_code == 200
    assert await locked_periods(org) == []


# --- History -----------------------------------------------------------------------------------


async def test_the_history_is_newest_first_with_actors_and_pages(client):
    owner, org = await practice(client)
    for month in (1, 2, 3):
        await lock_period(client, owner, org, "month", 2023, month)
    [first, *_] = (await get_state(client, owner, org))["periods"]
    await unlock_period(client, owner, org, first["id"], reason="Fix")

    rows = (await history(client, owner, org)).json()["data"]
    assert [r["event_type"] for r in rows] == ["reopened", "locked", "locked", "locked"]
    assert [r["period_label"] for r in rows] == [
        "January 2023",
        "March 2023",
        "February 2023",
        "January 2023",
    ]
    assert all(r["actor_user_id"] == str(owner.id) and r["actor_name"] for r in rows)

    page = await history(client, owner, org, limit=3)
    body = page.json()
    assert len(body["data"]) == 3 and body["meta"]["next_cursor"]
    rest = await history(client, owner, org, limit=3, cursor=body["meta"]["next_cursor"])
    assert [r["id"] for r in rest.json()["data"]] == [rows[3]["id"]]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 1000}, {"cursor": "garbage"}])
async def test_history_paging_parameters_are_validated(client, params):
    owner, org = await practice(client)
    assert (await history(client, owner, org, **params)).status_code == 422


# --- Permissions and isolation -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "can_read", "can_lock", "can_reopen"),
    [
        ("owner", True, True, True),
        ("admin", True, True, True),
        ("partner", True, True, False),
        ("manager", True, False, False),
        ("billing", True, False, False),
        ("staff", False, False, False),
        ("viewer", False, False, False),
    ],
)
async def test_permissions_per_role(client, role, can_read, can_lock, can_reopen):
    owner, org = await practice(client)
    actor = await staff(org, role)
    await lock_period(client, owner, org, "year", 2022)
    [existing] = (await get_state(client, owner, org))["periods"]

    def expected(allowed: bool) -> int:
        return 200 if allowed else 403

    state = await client.get(org_url(org, "/period-locks"), headers=actor.headers)
    assert state.status_code == expected(can_read), role
    assert (await history(client, actor, org)).status_code == expected(can_read), role
    lock = await lock_period(client, actor, org, "year", 2023)
    assert lock.status_code == expected(can_lock), role
    unlock = await unlock_period(client, actor, org, existing["id"], reason="Because")
    assert unlock.status_code == expected(can_reopen), role
    for denied in (state, lock, unlock):
        if denied.status_code == 403:
            assert error(denied)["code"] == "PERMISSION_DENIED"
    # A refusal writes nothing: the owner's lock, plus what the actor was allowed.
    assert len(await events(org)) == 1 + int(can_lock) + int(can_reopen)


async def test_permission_is_checked_before_the_body(client):
    _, org = await practice(client)
    viewer = await staff(org, "viewer")
    for path in ("/period-locks", "/period-locks/reopen"):
        response = await client.post(org_url(org, path), headers=viewer.headers, json={})
        assert response.status_code == 403, path


async def test_the_three_permissions_are_independent(client):
    owner, org = await practice(client)
    await lock_period(client, owner, org, "year", 2022)
    [existing] = (await get_state(client, owner, org))["periods"]
    await add_custom_role(org["id"], "Reopeners", ["period_lock.reopen"])
    actor = await staff(org, "Reopeners")
    assert (
        await client.get(org_url(org, "/period-locks"), headers=actor.headers)
    ).status_code == 403
    assert (await lock_period(client, actor, org, "year", 2023)).status_code == 403
    response = await unlock_period(client, actor, org, existing["id"], reason="Yes")
    assert response.status_code == 200


async def test_without_a_token_the_routes_answer_401(client):
    _, org = await practice(client)
    for method, path in (
        ("get", "/period-locks"),
        ("get", "/period-locks/history"),
        ("post", "/period-locks"),
        ("post", "/period-locks/reopen"),
    ):
        response = await client.request(method, org_url(org, path))
        assert response.status_code == 401, path


async def test_another_practice_answers_404_and_its_locks_are_separate(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other & Co")
    assert (await lock_period(client, owner, org, "year", 2023)).status_code == 200
    [period] = (await get_state(client, owner, org))["periods"]

    for actor, target in ((other_owner, org), (owner, other)):
        for method, path, body in (
            ("get", "/period-locks", None),
            ("get", "/period-locks/history", None),
            ("post", "/period-locks", {"period_type": "year", "year": 2022}),
            ("post", "/period-locks/reopen", {"lock_id": period["id"], "reason": "x"}),
        ):
            response = await client.request(
                method, org_url(target, path), headers=actor.headers, json=body
            )
            assert response.status_code == 404, (path, response.text)
    # The other practice cannot unlock this one's period through its own URL either.
    response = await unlock_period(client, other_owner, other, period["id"], reason="x")
    assert response.status_code == 404
    assert len(await locked_periods(org)) == 1 and await locked_periods(other) == []


# --- The history cannot be rewritten -----------------------------------------------------------


async def test_events_cannot_be_updated_or_deleted(client):
    owner, org = await practice(client)
    await lock_period(client, owner, org, "year", 2023)
    with pytest.raises(IntegrityError, match="append-only"):
        await sql(
            "UPDATE period_lock_events SET reason = 'x' WHERE organization_id = :o", o=org["id"]
        )
    with pytest.raises(IntegrityError, match="append-only"):
        await sql("DELETE FROM period_lock_events WHERE organization_id = :o", o=org["id"])
    assert len(await events(org)) == 1


async def test_locks_and_history_go_with_the_practice(client):
    owner, org = await practice(client)
    await lock_period(client, owner, org, "year", 2023)
    await sql("DELETE FROM organizations WHERE id = :o", o=org["id"])
    for table in ("locked_periods", "period_locks", "period_lock_events"):
        assert (
            await scalar(f"SELECT count(*) FROM {table} WHERE organization_id = :o", o=org["id"])  # noqa: S608
            == 0
        )


@pytest.mark.parametrize(
    ("period_type", "start", "end"),
    [
        ("day", "2023-01-01", "2023-01-01"),
        ("month", "2023-02-01", "2023-01-31"),
    ],
)
async def test_the_period_checks(client, period_type, start, end):
    _, org = await practice(client)
    with pytest.raises(IntegrityError):
        await sql(
            "INSERT INTO locked_periods (id, organization_id, period_type, start_date, end_date) "
            "VALUES (gen_random_uuid(), :o, :t, :s, :e)",
            o=org["id"],
            t=period_type,
            s=date.fromisoformat(start),
            e=date.fromisoformat(end),
        )


# --- Locking and writing do not overlap --------------------------------------------------------


async def test_concurrent_requests_for_the_same_period_lock_once(client):
    owner, org = await practice(client)
    responses = await asyncio.gather(
        *(lock_period(client, owner, org, "year", 2023) for _ in range(5))
    )
    assert sorted(r.status_code for r in responses) == [200, 422, 422, 422, 422]
    for response in responses:
        if response.status_code == 422:
            assert error(response)["code"] == "PERIOD_LOCK_OVERLAPS"
    assert len(await events(org)) == 1 and len(await locked_periods(org)) == 1
    assert len(await audit_entries(org["id"], "period_lock.locked")) == 1


async def test_concurrent_locks_of_different_periods_all_apply(client):
    owner, org = await practice(client)
    responses = await asyncio.gather(
        *(lock_period(client, owner, org, "month", 2023, m) for m in (1, 2, 3, 4))
    )
    assert {r.status_code for r in responses} == {200}
    assert len(await locked_periods(org)) == 4 and len(await events(org)) == 4


async def test_a_lock_waits_for_a_write_that_was_already_checked(client):
    owner, org = await practice(client)
    org_id = uuid.UUID(org["id"])
    day = date(2023, 3, 15)
    writer = get_sessionmaker()()
    try:
        # The write passes the check and keeps its share lock until it commits.
        await period_locks.assert_open(writer, org_id, day, "time")
        pending = asyncio.create_task(lock_period(client, owner, org, "year", 2023))
        await asyncio.sleep(0.5)
        assert not pending.done(), "the lock did not wait for the write"
        await writer.commit()
    finally:
        await writer.close()
    response = await asyncio.wait_for(pending, 10)
    assert response.status_code == 200, response.text
    # A write that starts after the lock is refused.
    async with get_sessionmaker()() as later:
        with pytest.raises(period_locks.PeriodLockedError):
            await period_locks.assert_open(later, org_id, day, "time")


async def test_a_write_that_starts_during_a_lock_waits_for_it_and_is_refused(client):
    _, org = await practice(client)
    org_id = uuid.UUID(org["id"])
    day = date(2023, 3, 15)

    async def write() -> None:
        async with get_sessionmaker()() as writer:
            await period_locks.assert_open_many(writer, org_id, [day], "expense")

    async with get_sessionmaker()() as locker:
        repo = PeriodLockRepository(locker)
        await repo.lock_row(org_id)
        await repo.add_period(
            LockedPeriod(
                organization_id=org_id,
                period_type="year",
                start_date=date(2023, 1, 1),
                end_date=date(2023, 12, 31),
            )
        )
        pending = asyncio.create_task(write())
        await asyncio.sleep(0.5)
        assert not pending.done(), "the write did not wait for the lock"
        await locker.commit()
    with pytest.raises(period_locks.PeriodLockedError):
        await asyncio.wait_for(pending, 10)


async def test_the_error_names_the_period_and_how_to_unlock_it(client):
    owner, org = await practice(client)
    await lock_period(client, owner, org, "quarter", 2023, 1)
    org_id = uuid.UUID(org["id"])
    async with get_sessionmaker()() as session:
        with pytest.raises(period_locks.PeriodLockedError) as caught:
            await period_locks.assert_open(session, org_id, date(2023, 2, 10), "time")
    assert "Q1 2023" in caught.value.message and "Unlock" in caught.value.message
    assert caught.value.details["locked_through"] == "2023-03-31"
    assert today(org) - timedelta(days=1) > date(2023, 3, 31)

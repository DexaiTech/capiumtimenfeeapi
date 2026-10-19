"""Period locks: the lock and reopen endpoints, the history, permissions, isolation, the
immutability of the history and the races between locking and writing."""

import asyncio
import uuid
from datetime import date, timedelta

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.exc import IntegrityError

from app.db.session import get_sessionmaker
from app.repositories.period_locks import PeriodLockRepository
from app.services import period_locks
from tests.clients_helpers import scalar, sql
from tests.domain_factories import practice, staff
from tests.factories import audit_entries, error
from tests.foundation_helpers import add_custom_role
from tests.period_lock_helpers import (
    events,
    lock_through,
    locked_through,
    org_url,
    reopen_to,
    set_lock,
    today,
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


# --- The state ---------------------------------------------------------------------------------


async def test_a_new_practice_has_nothing_locked(client):
    owner, org = await practice(client)
    assert await get_state(client, owner, org) == {
        "locked_through": None,
        "updated_at": None,
        "last_event": None,
    }
    page = await history(client, owner, org)
    assert page.status_code == 200
    assert page.json()["data"] == []
    assert page.json()["meta"] == {"next_cursor": None, "has_more": False}
    # The practice's row exists from the start: it is what every write takes a share lock on.
    count = await scalar(
        "SELECT count(*) FROM period_locks WHERE organization_id = :o", o=org["id"]
    )
    assert count == 1
    assert await locked_through(org) is None


async def test_locking_records_the_lock_an_event_and_an_audit_entry(client):
    owner, org = await practice(client)
    t = today(org)
    first = await lock_through(client, owner, org, t - 30 * DAY, reason="March close")
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["locked_through"] == str(t - 30 * DAY)
    assert data["updated_at"] is not None
    event = data["last_event"]
    assert event["event_type"] == "locked"
    assert event["previous_locked_through"] is None
    assert event["locked_through"] == str(t - 30 * DAY)
    assert event["reason"] == "March close"
    assert event["actor_user_id"] == str(owner.id) and event["actor_name"] == owner.full_name
    assert await get_state(client, owner, org) == data

    second = await lock_through(client, owner, org, t - 10 * DAY)
    assert second.status_code == 200, second.text
    event = second.json()["data"]["last_event"]
    assert event["previous_locked_through"] == str(t - 30 * DAY)
    assert event["locked_through"] == str(t - 10 * DAY) and event["reason"] is None
    assert await locked_through(org) == t - 10 * DAY
    assert len(await events(org)) == 2

    period_lock_id = str(
        await scalar("SELECT id FROM period_locks WHERE organization_id = :o", o=org["id"])
    )
    entries = await audit_entries(org["id"], "period_lock.locked")
    assert len(entries) == 2
    assert {(e["target_type"], e["target_id"], e["actor_user_id"]) for e in entries} == {
        ("period_lock", period_lock_id, str(owner.id))
    }
    assert entries[0]["metadata"] == {
        "event_type": "locked",
        "locked_through": str(t - 30 * DAY),
        "previous_locked_through": None,
        "reason": "March close",
        "changes": {"locked_through": [None, str(t - 30 * DAY)]},
    }
    assert entries[1]["metadata"]["changes"] == {
        "locked_through": [str(t - 30 * DAY), str(t - 10 * DAY)]
    }
    rows = await sql(
        "SELECT ip_address::text AS ip, request_id FROM audit_logs "
        "WHERE action = 'period_lock.locked'"
    )
    assert len(rows) == 2 and all(r["ip"] and r["request_id"] for r in rows)


async def test_the_lock_can_reach_today_but_not_tomorrow(client):
    owner, org = await practice(client)
    t = today(org)
    refused(
        await lock_through(client, owner, org, t + DAY), "PERIOD_LOCK_IN_FUTURE", "locked_through"
    )
    refused(
        await lock_through(client, owner, org, t + 400 * DAY),
        "PERIOD_LOCK_IN_FUTURE",
        "locked_through",
    )
    assert await locked_through(org) is None and await events(org) == []
    assert (await lock_through(client, owner, org, t)).status_code == 200
    assert await locked_through(org) == t


async def test_the_lock_only_moves_forward(client):
    owner, org = await practice(client)
    t = today(org)
    assert (await lock_through(client, owner, org, t - 20 * DAY)).status_code == 200
    for day in (t - 20 * DAY, t - 21 * DAY, t - 400 * DAY):
        refused(
            await lock_through(client, owner, org, day), "PERIOD_LOCK_NOT_FORWARD", "locked_through"
        )
    # Nothing was written by the refusals.
    assert await locked_through(org) == t - 20 * DAY
    assert len(await events(org)) == 1
    assert len(await audit_entries(org["id"], "period_lock.locked")) == 1
    assert (await lock_through(client, owner, org, t - 19 * DAY)).status_code == 200


@pytest.mark.parametrize(
    ("reason", "stored"),
    [(None, None), ("", None), ("   ", None), ("  Year end  ", "Year end")],
)
async def test_a_reason_is_optional_when_locking(client, reason, stored):
    owner, org = await practice(client)
    response = await lock_through(client, owner, org, today(org) - 5 * DAY, reason=reason)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["last_event"]["reason"] == stored
    assert (await events(org))[0]["reason"] == stored


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"locked_through": None},
        {"locked_through": "not-a-date"},
        {"locked_through": "2026-13-01"},
        {"locked_through": "1999-12-31"},
        {"locked_through": "2026-01-01", "reason": "x" * 501},
        {"locked_through": "2026-01-01", "reason": 5},
        {"locked_through": "2026-01-01", "unknown": 1},
    ],
)
async def test_lock_body_validation(client, body):
    owner, org = await practice(client)
    response = await client.post(org_url(org, "/period-locks"), headers=owner.headers, json=body)
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "VALIDATION_ERROR"
    assert await events(org) == [] and await locked_through(org) is None


# --- Reopening ---------------------------------------------------------------------------------


async def test_reopening_moves_the_lock_back_and_records_why(client):
    owner, org = await practice(client)
    admin = await staff(org, "admin")
    t = today(org)
    assert (await lock_through(client, owner, org, t - 10 * DAY)).status_code == 200

    back = await reopen_to(client, admin, org, t - 20 * DAY, reason="  Late supplier invoice  ")
    assert back.status_code == 200, back.text
    data = back.json()["data"]
    assert data["locked_through"] == str(t - 20 * DAY)
    event = data["last_event"]
    assert event["event_type"] == "reopened"
    assert event["previous_locked_through"] == str(t - 10 * DAY)
    assert event["locked_through"] == str(t - 20 * DAY)
    assert event["reason"] == "Late supplier invoice"
    assert event["actor_user_id"] == str(admin.id) and event["actor_name"] == admin.full_name

    nothing = await reopen_to(client, owner, org, None, reason="Start the year again")
    assert nothing.status_code == 200, nothing.text
    assert nothing.json()["data"]["locked_through"] is None
    last = nothing.json()["data"]["last_event"]
    assert (last["previous_locked_through"], last["locked_through"]) == (str(t - 20 * DAY), None)
    state = await get_state(client, owner, org)
    assert state["locked_through"] is None and state["last_event"]["event_type"] == "reopened"
    assert await locked_through(org) is None

    audit = await audit_entries(org["id"], "period_lock.reopened")
    assert [e["actor_user_id"] for e in audit] == [str(admin.id), str(owner.id)]
    assert audit[0]["metadata"] == {
        "event_type": "reopened",
        "locked_through": str(t - 20 * DAY),
        "previous_locked_through": str(t - 10 * DAY),
        "reason": "Late supplier invoice",
        "changes": {"locked_through": [str(t - 10 * DAY), str(t - 20 * DAY)]},
    }
    assert audit[1]["metadata"]["locked_through"] is None


@pytest.mark.parametrize("body", [{}, {"reason": None}, {"reason": ""}, {"reason": "   "}])
async def test_reopening_needs_a_reason(client, body):
    owner, org = await practice(client)
    t = today(org)
    await lock_through(client, owner, org, t - 10 * DAY)
    refused(
        await reopen_to(client, owner, org, t - 20 * DAY, **body),
        "PERIOD_LOCK_REASON_REQUIRED",
        "reason",
    )
    assert await locked_through(org) == t - 10 * DAY
    assert len(await events(org)) == 1
    assert await audit_entries(org["id"], "period_lock.reopened") == []


async def test_reopening_must_go_back(client):
    owner, org = await practice(client)
    t = today(org)
    await lock_through(client, owner, org, t - 10 * DAY)
    for day in (t - 10 * DAY, t - 5 * DAY, t):
        refused(
            await reopen_to(client, owner, org, day, reason="No"),
            "PERIOD_LOCK_NOT_BACKWARD",
            "reopen_to",
        )
    assert await locked_through(org) == t - 10 * DAY and len(await events(org)) == 1

    assert (await reopen_to(client, owner, org, None, reason="Clear it")).status_code == 200
    # With nothing locked there is nothing to reopen: neither a date nor null will do.
    for day in (None, t - 50 * DAY):
        refused(
            await reopen_to(client, owner, org, day, reason="Again"),
            "PERIOD_LOCK_NOT_BACKWARD",
            "reopen_to",
        )
    assert len(await events(org)) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"reason": "oops"},
        {"reopen_to": "not-a-date", "reason": "x"},
        {"reopen_to": "1999-12-31", "reason": "x"},
        {"reopen_to": None, "reason": "x" * 501},
        {"reopen_to": None, "reason": "x", "unknown": 1},
    ],
)
async def test_reopen_body_validation_never_unlocks(client, body):
    owner, org = await practice(client)
    t = today(org)
    await lock_through(client, owner, org, t - 10 * DAY)
    response = await client.post(
        org_url(org, "/period-locks/reopen"), headers=owner.headers, json=body
    )
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "VALIDATION_ERROR"
    # An omitted `reopen_to` is not the same as null: it must never remove the lock.
    assert await locked_through(org) == t - 10 * DAY and len(await events(org)) == 1


async def test_a_reopened_period_can_be_locked_again(client):
    owner, org = await practice(client)
    t = today(org)
    await lock_through(client, owner, org, t - 10 * DAY)
    await reopen_to(client, owner, org, None, reason="Redo")
    assert (await lock_through(client, owner, org, t - 20 * DAY)).status_code == 200
    assert [e["event_type"] for e in await events(org)] == ["locked", "reopened", "locked"]
    assert (await events(org))[2]["previous_locked_through"] is None


# --- The history -------------------------------------------------------------------------------


async def test_the_history_is_newest_first_with_actors_and_pages(client):
    owner, org = await practice(client)
    admin = await staff(org, "admin")
    t = today(org)
    steps = [
        (lock_through, owner, t - 50 * DAY, {}),
        (lock_through, admin, t - 40 * DAY, {"reason": "Q1"}),
        (reopen_to, admin, t - 45 * DAY, {"reason": "Missed invoices"}),
        (lock_through, owner, t - 30 * DAY, {}),
        (reopen_to, owner, None, {"reason": "Audit adjustment"}),
    ]
    for action, actor, day, extra in steps:
        assert (await action(client, actor, org, day, **extra)).status_code == 200

    full = await history(client, owner, org)
    items = full.json()["data"]
    assert [(i["event_type"], i["locked_through"]) for i in items] == [
        ("reopened", None),
        ("locked", str(t - 30 * DAY)),
        ("reopened", str(t - 45 * DAY)),
        ("locked", str(t - 40 * DAY)),
        ("locked", str(t - 50 * DAY)),
    ]
    assert [i["previous_locked_through"] for i in items] == [
        str(t - 30 * DAY),
        str(t - 45 * DAY),
        str(t - 40 * DAY),
        str(t - 50 * DAY),
        None,
    ]
    assert [i["actor_name"] for i in items] == [
        owner.full_name,
        owner.full_name,
        admin.full_name,
        admin.full_name,
        owner.full_name,
    ]
    assert [i["reason"] for i in items] == ["Audit adjustment", None, "Missed invoices", "Q1", None]
    assert full.json()["meta"]["has_more"] is False
    assert (await get_state(client, owner, org))["last_event"] == items[0]

    seen: list[str] = []
    cursor = None
    for expected_more in (True, True, False):
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await history(client, owner, org, **params)).json()
        seen += [i["id"] for i in page["data"]]
        assert page["meta"]["has_more"] is expected_more
        cursor = page["meta"]["next_cursor"]
    assert seen == [i["id"] for i in items]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"cursor": "garbage"}])
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
    _, org = await practice(client)
    actor = await staff(org, role)
    t = today(org)
    await set_lock(org, t - 10 * DAY)

    def expected(allowed: bool) -> int:
        return 200 if allowed else 403

    state = await client.get(org_url(org, "/period-locks"), headers=actor.headers)
    assert state.status_code == expected(can_read), role
    assert (await history(client, actor, org)).status_code == expected(can_read), role
    lock = await lock_through(client, actor, org, t - 5 * DAY)
    assert lock.status_code == expected(can_lock), role
    reopen = await reopen_to(client, actor, org, t - 20 * DAY, reason="Because")
    assert reopen.status_code == expected(can_reopen), role
    for denied in (state, lock, reopen):
        if denied.status_code == 403:
            assert error(denied)["code"] == "PERMISSION_DENIED"
    # A refusal writes nothing.
    assert len(await events(org)) == int(can_lock) + int(can_reopen)


async def test_permission_is_checked_before_the_body(client):
    _, org = await practice(client)
    viewer = await staff(org, "viewer")
    for path in ("/period-locks", "/period-locks/reopen"):
        response = await client.post(org_url(org, path), headers=viewer.headers, json={})
        assert response.status_code == 403, path


async def test_the_three_permissions_are_independent(client):
    _, org = await practice(client)
    t = today(org)
    await set_lock(org, t - 10 * DAY)
    await add_custom_role(org["id"], "Reopeners", ["period_lock.reopen"])
    actor = await staff(org, "Reopeners")
    assert (
        await client.get(org_url(org, "/period-locks"), headers=actor.headers)
    ).status_code == 403
    assert (await lock_through(client, actor, org, t - 5 * DAY)).status_code == 403
    assert (await reopen_to(client, actor, org, t - 20 * DAY, reason="Yes")).status_code == 200


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


async def test_another_practice_answers_404_and_its_lock_is_separate(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other & Co")
    t = today(org)
    assert (await lock_through(client, owner, org, t - 10 * DAY)).status_code == 200

    for actor, target in ((other_owner, org), (owner, other)):
        for method, path, body in (
            ("get", "/period-locks", None),
            ("get", "/period-locks/history", None),
            ("post", "/period-locks", {"locked_through": str(t - 20 * DAY)}),
            ("post", "/period-locks/reopen", {"reopen_to": None, "reason": "Peek"}),
        ):
            response = await client.request(
                method, org_url(target, path), headers=actor.headers, json=body
            )
            assert response.status_code == 404, (method, path)
            assert error(response)["code"] == "RESOURCE_NOT_FOUND"
    # The refusals changed nothing, and the other practice is open.
    assert await locked_through(org) == t - 10 * DAY and len(await events(org)) == 1
    assert (await get_state(client, other_owner, other))["locked_through"] is None
    assert await events(other) == []
    assert (await lock_through(client, other_owner, other, t - 30 * DAY)).status_code == 200
    assert await locked_through(org) == t - 10 * DAY


# --- The history cannot be changed -------------------------------------------------------------


async def test_events_cannot_be_updated_or_deleted(client):
    owner, org = await practice(client)
    t = today(org)
    await lock_through(client, owner, org, t - 10 * DAY, reason="Close")
    await reopen_to(client, owner, org, t - 20 * DAY, reason="Reopen")
    before = await events(org)
    assert len(before) == 2

    for statement in (
        "UPDATE period_lock_events SET reason = 'edited'",
        "UPDATE period_lock_events SET locked_through = locked_through - 1 WHERE event_type = 'locked'",
        "UPDATE period_lock_events SET created_at = now()",
        "DELETE FROM period_lock_events",
        "DELETE FROM period_lock_events WHERE event_type = 'reopened'",
    ):
        with pytest.raises(IntegrityError, match="append-only"):
            await sql(statement)
    assert await events(org) == before

    # No endpoint changes them either.
    for method in ("put", "patch", "delete"):
        response = await client.request(
            method, org_url(org, "/period-locks/history"), headers=owner.headers, json={}
        )
        assert response.status_code in (404, 405), method


async def test_the_lock_row_and_its_history_go_with_the_practice(client):
    owner, org = await practice(client)
    await lock_through(client, owner, org, today(org) - 10 * DAY)
    # Only the practice's own deletion removes events: the cascade is not a direct DELETE.
    await sql("DELETE FROM organizations WHERE id = :o", o=org["id"])
    assert await events(org) == []
    assert await locked_through(org) is None


@pytest.mark.parametrize(
    ("event_type", "previous", "new", "reason", "constraint"),
    [
        ("locked", None, None, None, "ck_period_lock_events_locked_moves_forward"),
        ("locked", "2026-03-31", "2026-03-31", None, "ck_period_lock_events_locked_moves_forward"),
        ("locked", "2026-03-31", "2026-03-30", None, "ck_period_lock_events_locked_moves_forward"),
        ("reopened", "2026-03-31", "2026-03-30", None, "ck_period_lock_events_reopen_reason"),
        ("reopened", None, None, "why", "ck_period_lock_events_reopened_moves_back"),
        (
            "reopened",
            "2026-03-31",
            "2026-03-31",
            "why",
            "ck_period_lock_events_reopened_moves_back",
        ),
        (
            "reopened",
            "2026-03-31",
            "2026-04-01",
            "why",
            "ck_period_lock_events_reopened_moves_back",
        ),
        ("frozen", None, "2026-03-31", None, "ck_period_lock_events_event_type"),
        ("locked", None, "2026-03-31", "", "ck_period_lock_events_reason"),
        ("locked", None, "2026-03-31", "x" * 501, "ck_period_lock_events_reason"),
    ],
)
async def test_the_event_checks(client, event_type, previous, new, reason, constraint):
    owner, org = await practice(client)
    with pytest.raises(IntegrityError, match=constraint):
        await sql(
            "INSERT INTO period_lock_events (id, organization_id, event_type, "
            "previous_locked_through, locked_through, reason, actor_user_id) "
            "VALUES (gen_random_uuid(), :o, :t, :p, :n, :r, :a)",
            o=org["id"],
            t=event_type,
            p=None if previous is None else date.fromisoformat(previous),
            n=None if new is None else date.fromisoformat(new),
            r=reason,
            a=str(owner.id),
        )
    assert await events(org) == []


async def test_the_actor_and_the_lock_must_belong_to_the_practice(client):
    _, org = await practice(client)
    stranger, _ = await practice(client, "Other & Co")
    insert = (
        "INSERT INTO period_lock_events (id, organization_id, event_type, locked_through, "
        "actor_user_id) VALUES (gen_random_uuid(), :o, 'locked', '2026-03-31', :a)"
    )
    with pytest.raises(IntegrityError, match="fk_period_lock_events_organization_id_actor_members"):
        await sql(insert, o=org["id"], a=str(stranger.id))
    # One row per practice.
    with pytest.raises(IntegrityError, match="uq_period_locks_organization_id"):
        await sql(
            "INSERT INTO period_locks (id, organization_id) VALUES (gen_random_uuid(), :o)",
            o=org["id"],
        )


# --- Locking and writing do not overlap --------------------------------------------------------


async def test_concurrent_requests_for_the_same_date_lock_once(client):
    owner, org = await practice(client)
    t = today(org)
    responses = await asyncio.gather(
        *(lock_through(client, owner, org, t - 5 * DAY) for _ in range(5))
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 422, 422, 422, 422]
    for response in responses:
        if response.status_code == 422:
            assert error(response)["code"] == "PERIOD_LOCK_NOT_FORWARD"
    assert len(await events(org)) == 1 and await locked_through(org) == t - 5 * DAY
    assert len(await audit_entries(org["id"], "period_lock.locked")) == 1


async def test_concurrent_locks_of_different_dates_form_one_chain(client):
    owner, org = await practice(client)
    t = today(org)
    days = [t - 30 * DAY, t - 20 * DAY, t - 10 * DAY, t - 25 * DAY]
    responses = await asyncio.gather(*(lock_through(client, owner, org, d) for d in days))
    assert {r.status_code for r in responses} <= {200, 422}
    # The latest date always wins; every accepted lock chains onto the one before it.
    assert await locked_through(org) == t - 10 * DAY
    rows = await events(org)
    assert len(rows) == sum(r.status_code == 200 for r in responses)
    previous = None
    for row in rows:
        assert row["previous_locked_through"] == previous
        assert previous is None or row["locked_through"] > previous
        previous = row["locked_through"]
    assert previous == t - 10 * DAY


async def test_a_lock_waits_for_a_write_that_was_already_checked(client):
    owner, org = await practice(client)
    org_id = uuid.UUID(org["id"])
    t = today(org)
    writer = get_sessionmaker()()
    try:
        # The write passes the check and keeps its share lock until it commits.
        await period_locks.assert_open(writer, org_id, t - 3 * DAY, "time")
        pending = asyncio.create_task(lock_through(client, owner, org, t - DAY))
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
            await period_locks.assert_open(later, org_id, t - 3 * DAY, "time")


async def test_a_write_that_starts_during_a_lock_waits_for_it_and_is_refused(client):
    _, org = await practice(client)
    org_id = uuid.UUID(org["id"])
    t = today(org)

    async def write() -> None:
        async with get_sessionmaker()() as writer:
            await period_locks.assert_open_many(writer, org_id, [t - 3 * DAY], "expense")

    async with get_sessionmaker()() as locker:
        row = await PeriodLockRepository(locker).lock_row(org_id)
        row.locked_through = t - DAY
        await locker.flush()
        pending = asyncio.create_task(write())
        await asyncio.sleep(0.5)
        assert not pending.done(), "the write did not wait for the lock"
        await locker.commit()
    with pytest.raises(period_locks.PeriodLockedError):
        await asyncio.wait_for(pending, 10)

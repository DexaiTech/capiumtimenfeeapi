"""The ticket list: sorting, keyset pagination, search, filters, the shape of an item."""

import base64
import json
import uuid

import pytest

from tests.clients_helpers import count_queries, sql
from tests.factories import error
from tests.ticket_helpers import (
    ITEM_KEYS,
    insert_ticket,
    make_ticket,
    new_team,
    tag_id,
    ticket_url,
    upload,
    utc,
)
from tests.time_helpers import ok_data


async def seeded(client):
    """Five tickets on five days (12:00 UTC), with every status and priority once or twice."""
    t = await new_team(client)
    alice, bob = await t.member("staff"), await t.member("manager")

    def make(number, day, subject, status, priority, assignee, tags, description=None):
        return insert_ticket(
            t.org,
            t.owner,
            created_at=utc(2026, 9, day),
            status=status,
            priority=priority,
            subject=subject,
            number=number,
            assignee=assignee,
            tags=tags,
            description=description,
        )

    ids = {
        1001: await make(1001, 1, "Alpha login bug", "open", "high", alice, ["Bug"]),
        1002: await make(
            1002,
            2,
            "beta report slow",
            "on_hold",
            "medium",
            bob,
            ["Reporting", "Bug"],
            "Slow when exporting the TRIAL BALANCE",
        ),
        1003: await make(1003, 3, "Gamma 100% wrong", "in_progress", "low", None, ["Accounts"]),
        1004: await make(1004, 4, "delta_under score", "reopened", "not_set", alice, []),
        1005: await make(
            1005, 5, "Epsilon trial balance", "closed", "high", None, ["Trial Balance"]
        ),
    }
    return t, alice, bob, ids


async def numbers(client, t, actor=None, **params) -> list[int]:
    response = await client.get(
        ticket_url(t.org), headers=(actor or t.owner).headers, params=params
    )
    return [item["number"] for item in ok_data(response)]


# --- Sorting --------------------------------------------------------------------------------

BY_NUMBER = [1001, 1002, 1003, 1004, 1005]


@pytest.mark.parametrize(
    ("sort", "order", "expected"),
    [
        (None, None, BY_NUMBER[::-1]),
        ("created_at", None, BY_NUMBER[::-1]),
        ("created_at", "asc", BY_NUMBER),
        ("number", None, BY_NUMBER[::-1]),
        ("number", "asc", BY_NUMBER),
        ("code", "asc", BY_NUMBER),
        ("code", "desc", BY_NUMBER[::-1]),
        ("subject", None, [1001, 1002, 1004, 1005, 1003]),
        ("subject", "desc", [1003, 1005, 1004, 1002, 1001]),
        ("status", None, [1005, 1003, 1002, 1001, 1004]),
        ("status", "desc", [1004, 1001, 1002, 1003, 1005]),
    ],
)
async def test_sorting(client, sort, order, expected):
    t, *_ = await seeded(client)
    params = {k: v for k, v in (("sort", sort), ("order", order)) if v}
    assert await numbers(client, t, **params) == expected


async def test_sorting_by_priority_puts_high_first(client):
    t, *_ = await seeded(client)
    response = await client.get(
        ticket_url(t.org), headers=t.owner.headers, params={"sort": "priority"}
    )
    data = ok_data(response)
    assert [i["priority"] for i in data] == ["high", "high", "medium", "low", "not_set"]
    descending = ok_data(
        await client.get(
            ticket_url(t.org), headers=t.owner.headers, params={"sort": "priority", "order": "desc"}
        )
    )
    assert [i["priority"] for i in descending] == ["not_set", "low", "medium", "high", "high"]


# --- Pagination -----------------------------------------------------------------------------


async def pages(client, t, limit=2, **params):
    """Every item of the list, page by page; also the number of pages."""
    seen, cursor, calls = [], None, 0
    while True:
        query = {**params, "limit": limit, **({"cursor": cursor} if cursor else {})}
        body = (await client.get(ticket_url(t.org), headers=t.owner.headers, params=query)).json()
        assert body["success"] is True
        seen += [item["id"] for item in body["data"]]
        calls += 1
        cursor = body["meta"]["next_cursor"]
        assert body["meta"]["has_more"] == (cursor is not None)
        if cursor is None:
            return seen, calls


SORT_CASES = [
    {},
    {"sort": "number", "order": "asc"},
    {"sort": "subject"},
    {"sort": "subject", "order": "desc"},
    {"sort": "status"},
    {"sort": "priority"},
    {"sort": "priority", "order": "desc"},
    {"sort": "created_at", "order": "asc"},
]


@pytest.mark.parametrize("params", SORT_CASES)
async def test_pages_reach_every_ticket_once_in_the_same_order(client, params):
    t, *_ = await seeded(client)
    everything = ok_data(
        await client.get(
            ticket_url(t.org), headers=t.owner.headers, params={**params, "limit": 100}
        )
    )
    seen, calls = await pages(client, t, limit=2, **params)
    assert calls == 3
    assert seen == [item["id"] for item in everything]
    assert len(set(seen)) == 5


@pytest.mark.parametrize("params", SORT_CASES)
async def test_ties_do_not_lose_or_repeat_tickets_across_pages(client, params):
    """Seven tickets with the same time, subject, status and priority: only the id tells them
    apart, and pages of three must still reach each exactly once."""
    t = await new_team(client)
    ids = {
        await insert_ticket(
            t.org, t.owner, created_at=utc(2026, 9, 1), subject="Same", number=2000 + i
        )
        for i in range(7)
    }
    seen, calls = await pages(client, t, limit=3, **params)
    assert calls == 3
    assert len(seen) == 7 and set(seen) == ids


async def test_the_last_page_has_no_cursor(client):
    t, *_ = await seeded(client)
    body = (
        await client.get(ticket_url(t.org), headers=t.owner.headers, params={"limit": 5})
    ).json()
    assert len(body["data"]) == 5
    assert body["meta"] == {"next_cursor": None, "has_more": False}
    empty = await new_team(client, "Empty Practice")
    body = (await client.get(ticket_url(empty.org), headers=empty.owner.headers)).json()
    assert body["data"] == [] and body["meta"]["has_more"] is False


def _cursor(**payload) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


async def test_bad_cursors_answer_422(client):
    t, *_ = await seeded(client)
    first = (
        await client.get(
            ticket_url(t.org), headers=t.owner.headers, params={"limit": 1, "sort": "subject"}
        )
    ).json()["meta"]["next_cursor"]
    some_id = str(uuid.uuid4())
    cursors = [
        ("garbage", {}),
        ("!!!", {}),
        (first, {"sort": "number"}),  # taken from another sort
        (first, {"sort": "subject", "order": "desc"}),  # and another direction
        (_cursor(s="number", d=True, k="raw", v="x", id=some_id), {}),  # not a number
        (_cursor(s="number", d=True, k="raw", v=0, id=some_id), {"sort": "number"}),
        (_cursor(s="created_at", d=True, k="raw", v="2026-01-01", id=some_id), {}),
        (_cursor(s="created_at", d=True, k="dt", v="2026-01-01T00:00:00", id=some_id), {}),
        (_cursor(s="created_at", d=True, k="dt", v="not a time", id=some_id), {}),
        (_cursor(s="created_at", d=True, k="dt", v="2026-01-01T00:00:00+00:00", id="x"), {}),
        (_cursor(s="status", d=False, k="raw", v="shipped", id=some_id), {"sort": "status"}),
        (_cursor(s="priority", d=False, k="raw", v=9, id=some_id), {"sort": "priority"}),
        (_cursor(s="subject", d=False, k="raw", v="a\u0000b", id=some_id), {"sort": "subject"}),
        (_cursor(s="subject", d=False, k="raw", v=5, id=some_id), {"sort": "subject"}),
        (_cursor(s="subject", d=False, k="dt", v="a", id=some_id), {"sort": "subject"}),
        (_cursor(s="subject"), {"sort": "subject"}),
        ("e30", {}),  # {}
    ]
    for cursor, extra in cursors:
        response = await client.get(
            ticket_url(t.org), headers=t.owner.headers, params={"cursor": cursor, **extra}
        )
        assert response.status_code == 422, (cursor, extra, response.text)
        assert error(response)["details"][0]["field"] == "cursor"


async def test_a_good_cursor_continues_where_it_left_off(client):
    t, *_ = await seeded(client)
    params = {"limit": 2, "sort": "subject"}
    first = (await client.get(ticket_url(t.org), headers=t.owner.headers, params=params)).json()
    second = (
        await client.get(
            ticket_url(t.org),
            headers=t.owner.headers,
            params={**params, "cursor": first["meta"]["next_cursor"]},
        )
    ).json()
    assert [i["number"] for i in first["data"]] == [1001, 1002]
    assert [i["number"] for i in second["data"]] == [1004, 1005]


# --- Filters --------------------------------------------------------------------------------


async def test_filter_by_status_and_priority(client):
    t, *_ = await seeded(client)
    assert await numbers(client, t, status="open") == [1001]
    assert await numbers(client, t, status=["open", "on_hold"]) == [1002, 1001]
    assert await numbers(client, t, status=["open", "closed", "reopened"]) == [1005, 1004, 1001]
    assert await numbers(client, t, priority="high") == [1005, 1001]
    assert await numbers(client, t, priority=["low", "not_set"]) == [1004, 1003]
    assert await numbers(client, t, status="closed", priority="high") == [1005]
    assert await numbers(client, t, status="open", priority="low") == []


async def test_filter_by_tag_matches_any_of_the_tags(client):
    t, *_ = await seeded(client)
    bug, accounts = await tag_id(t.org, "Bug"), await tag_id(t.org, "Accounts")
    ui = await tag_id(t.org, "UI")
    assert await numbers(client, t, tag_id=bug) == [1002, 1001]
    assert await numbers(client, t, tag_id=[bug, accounts]) == [1003, 1002, 1001]
    assert await numbers(client, t, tag_id=ui) == []
    # A ticket with two matching tags is listed once.
    reporting = await tag_id(t.org, "Reporting")
    assert await numbers(client, t, tag_id=[bug, reporting]) == [1002, 1001]
    # Another practice's tag matches nothing and is no error.
    other = await new_team(client, "Other Practice")
    assert await numbers(client, t, tag_id=await tag_id(other.org, "Bug")) == []


async def test_filter_by_assignee(client):
    t, alice, bob, _ = await seeded(client)
    assert await numbers(client, t, assignee_id=str(alice.id)) == [1004, 1001]
    assert await numbers(client, t, assignee_id=str(bob.id)) == [1002]
    assert await numbers(client, t, unassigned="true") == [1005, 1003]
    assert await numbers(client, t, assignee_id=str(uuid.uuid4())) == []
    assert await numbers(client, t, assignee_id=str(alice.id), status="reopened") == [1004]


async def test_filter_by_created_dates_is_inclusive(client):
    t, *_ = await seeded(client)
    assert await numbers(client, t, created_from="2026-09-02", created_to="2026-09-04") == [
        1004,
        1003,
        1002,
    ]
    assert await numbers(client, t, created_from="2026-09-04") == [1005, 1004]
    assert await numbers(client, t, created_to="2026-09-02") == [1002, 1001]
    assert await numbers(client, t, created_from="2026-09-03", created_to="2026-09-03") == [1003]
    assert await numbers(client, t, created_from="2026-10-01") == []


async def test_created_dates_are_the_practices_calendar_days(client):
    t = await new_team(client)  # Europe/London: UTC+1 in September
    late = await insert_ticket(t.org, t.owner, created_at=utc(2026, 9, 2, 23, 30), number=1)
    before_midnight = await insert_ticket(
        t.org, t.owner, created_at=utc(2026, 9, 3, 22, 59), number=2
    )
    midnight = await insert_ticket(t.org, t.owner, created_at=utc(2026, 9, 3, 23, 0), number=3)
    assert late and before_midnight and midnight
    # 23:30 UTC on the 2nd is 00:30 on the 3rd in London; 23:00 UTC on the 3rd is 00:00 on the 4th.
    assert await numbers(client, t, created_from="2026-09-03", created_to="2026-09-03") == [2, 1]
    assert await numbers(client, t, created_to="2026-09-02") == []
    assert await numbers(client, t, created_from="2026-09-04") == [3]
    await sql("UPDATE organizations SET timezone = 'Pacific/Auckland' WHERE id = :o", o=t.id)
    # Auckland is UTC+12 then: 12:00 UTC on the 2nd... all three fall on the 3rd and 4th.
    assert await numbers(client, t, created_from="2026-09-03", created_to="2026-09-03") == [1]
    assert await numbers(client, t, created_from="2026-09-04", created_to="2026-09-04") == [3, 2]


async def test_search_looks_at_code_subject_and_description(client):
    t, *_ = await seeded(client)
    assert await numbers(client, t, q="login") == [1001]
    assert await numbers(client, t, q="  LOGIN  ") == [1001]
    assert await numbers(client, t, q="APT1003") == [1003]
    assert await numbers(client, t, q="apt100") == BY_NUMBER[::-1]
    assert await numbers(client, t, q="trial balance") == [1005, 1002]  # subject, description
    assert await numbers(client, t, q="exporting") == [1002]
    assert await numbers(client, t, q="nothing like it") == []
    assert await numbers(client, t, q="trial", status="closed") == [1005]
    assert await numbers(client, t, q="") == BY_NUMBER[::-1]


async def test_search_treats_wildcards_and_quotes_as_plain_text(client):
    t, *_ = await seeded(client)
    assert await numbers(client, t, q="%") == [1003]
    assert await numbers(client, t, q="100%") == [1003]
    assert await numbers(client, t, q="_") == [1004]
    assert await numbers(client, t, q="\\") == []
    assert await numbers(client, t, q="'; DROP TABLE tickets; --") == []
    assert await numbers(client, t, q="lo\x00gin") == [1001]  # the NUL is dropped
    assert len(await sql("SELECT 1 FROM tickets")) == 5


@pytest.mark.parametrize(
    "params",
    [
        {"status": "bogus"},
        {"status": ["open", "bogus"]},
        {"priority": "bogus"},
        {"assignee_id": "nope"},
        {"tag_id": "nope"},
        {"created_from": "2026-13-01"},
        {"created_to": "banana"},
        {"sort": "bogus"},
        {"sort": "description"},
        {"order": "sideways"},
        {"unassigned": "maybe"},
        {"limit": 0},
        {"limit": 101},
        {"limit": "ten"},
        {"q": "x" * 201},
        {"status": ["open"] * 11},
    ],
)
async def test_list_parameter_validation(client, params):
    t, *_ = await seeded(client)
    response = await client.get(ticket_url(t.org), headers=t.owner.headers, params=params)
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "VALIDATION_ERROR"


async def test_list_rules_between_parameters(client):
    t, alice, *_ = await seeded(client)
    backwards = await client.get(
        ticket_url(t.org),
        headers=t.owner.headers,
        params={"created_from": "2026-09-05", "created_to": "2026-09-01"},
    )
    assert backwards.status_code == 422
    assert error(backwards)["details"][0]["field"] == "created_from"
    both = await client.get(
        ticket_url(t.org),
        headers=t.owner.headers,
        params={"assignee_id": str(alice.id), "unassigned": "true"},
    )
    assert both.status_code == 422
    assert error(both)["details"][0]["field"] == "unassigned"


# --- The items ------------------------------------------------------------------------------


async def test_a_list_item_has_the_documented_shape(client):
    t = await new_team(client)
    staff = await t.member("staff")
    bug, ui = await tag_id(t.org, "Bug"), await tag_id(t.org, "UI")
    long_text = "word " * 100
    created = await make_ticket(
        client,
        t.owner,
        t.org,
        "Shape of an item",
        description=long_text,
        priority="medium",
        status="in_progress",
        tag_ids=[ui, bug],
        assignee_id=str(staff.id),
    )
    [item] = ok_data(await client.get(ticket_url(t.org), headers=t.owner.headers))
    assert set(item) == ITEM_KEYS
    assert item["id"] == created["id"]
    assert item["code"] == "APT1" and item["number"] == 1 and item["category"] == "application"
    assert item["description_excerpt"] == long_text.strip()[:200]
    assert len(item["description_excerpt"]) == 200
    assert [tag["name"] for tag in item["tags"]] == ["Bug", "UI"]
    assert set(item["tags"][0]) == {"id", "name", "color"}
    assert item["assignee"] == {"id": str(staff.id), "full_name": staff.full_name}
    assert item["created_by"] == str(t.owner.id) and item["created_by_name"] == t.owner.full_name
    assert (item["comment_count"], item["attachment_count"]) == (0, 0)
    assert item["closed_at"] is None


async def test_a_short_or_missing_description_excerpt(client):
    t = await new_team(client)
    await make_ticket(client, t.owner, t.org, "With", description="Short and sweet")
    await make_ticket(client, t.owner, t.org, "Without")
    items = ok_data(await client.get(ticket_url(t.org), headers=t.owner.headers))
    by_subject = {i["subject"]: i for i in items}
    assert by_subject["With"]["description_excerpt"] == "Short and sweet"
    assert by_subject["Without"]["description_excerpt"] is None


async def test_the_list_counts_comments_and_all_files(client):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org)
    other = await make_ticket(client, t.owner, t.org, "Quiet one")
    url = ticket_url(t.org, ticket["id"])
    for body in ("First", "Second"):
        created = await client.post(url + "/comments", headers=t.owner.headers, json={"body": body})
        assert created.status_code == 201
    comment_id = created.json()["data"]["id"]
    assert (await upload(client, t.owner, t.org, ticket["id"])).status_code == 201
    on_comment = await upload(client, t.owner, t.org, ticket["id"], comment_id=comment_id)
    assert on_comment.status_code == 201
    items = {
        i["id"]: i for i in ok_data(await client.get(ticket_url(t.org), headers=t.owner.headers))
    }
    assert (items[ticket["id"]]["comment_count"], items[ticket["id"]]["attachment_count"]) == (2, 2)
    assert (items[other["id"]]["comment_count"], items[other["id"]]["attachment_count"]) == (0, 0)


async def test_the_list_costs_the_same_queries_whatever_the_page_size(client):
    t = await new_team(client)
    bug = await tag_id(t.org, "Bug")
    staff = await t.member("staff")
    for number in range(1, 26):
        await insert_ticket(
            t.org,
            t.owner,
            created_at=utc(2026, 9, 1),
            number=number,
            assignee=staff,
            tags=["Bug"] if number % 2 else [],
        )
    assert bug
    with count_queries() as small:
        assert (
            len(
                ok_data(
                    await client.get(
                        ticket_url(t.org), headers=t.owner.headers, params={"limit": 2}
                    )
                )
            )
            == 2
        )
    with count_queries() as large:
        assert (
            len(
                ok_data(
                    await client.get(
                        ticket_url(t.org), headers=t.owner.headers, params={"limit": 25}
                    )
                )
            )
            == 25
        )
    assert len(small) == len(large)
    assert len(large) <= 8


async def test_the_list_belongs_to_the_practice(client):
    mine = await new_team(client)
    theirs = await new_team(client, "Their Practice")
    await make_ticket(client, mine.owner, mine.org, "Mine")
    await make_ticket(client, theirs.owner, theirs.org, "Theirs")
    items = ok_data(await client.get(ticket_url(mine.org), headers=mine.owner.headers))
    assert [i["subject"] for i in items] == ["Mine"]

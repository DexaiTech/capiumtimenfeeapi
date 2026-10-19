"""Expense lists: the claim list (filters, sorts, pages), the summary and the line list."""

from datetime import UTC, date, datetime
from decimal import Decimal

from tests.billing_helpers import set_org
from tests.domain_factories import make_client, make_job
from tests.expense_helpers import (
    claim_url,
    expense_team,
    make_claim,
    make_line,
    type_id,
    upload_receipt,
)
from tests.factories import add_member, error
from tests.foundation_helpers import add_custom_role
from tests.time_helpers import err, ok_data, set_manager


def at(hour: int) -> datetime:
    return datetime(2026, 9, 12, hour, 0, tzinfo=UTC)


async def build(client):
    """Alice's two claims, Bob's two, an empty draft of the Owner, and the clients around them."""
    team = await expense_team(client)
    org = team.org
    alice = await add_member(org["id"], "staff", full_name="Alice Adams")
    bob = await add_member(org["id"], "staff", full_name="Bob Brown")
    acme = await make_client(org, "Acme Ltd")
    beta = await make_client(org, "Beta Ltd")
    job = await make_job(org, beta, "Accounts")
    ids = {}
    ids["a1"] = await make_claim(org, alice, title="Alice draft")
    await make_line(org, ids["a1"], net=1000, client=acme, incurred_on=date(2026, 9, 1))
    ids["a2"] = await make_claim(
        org, alice, status="submitted", title="Alice submitted", submitted_at=at(12)
    )
    await make_line(
        org,
        ids["a2"],
        code="PRINTING",
        net=3000,
        vat=600,
        client=beta,
        job=job,
        is_billable=True,
        recharge_amount=3300,
        incurred_on=date(2026, 9, 10),
    )
    ids["b1"] = await make_claim(
        org,
        bob,
        status="approved",
        title="Bob approved",
        submitted_at=at(11),
        decided_by=team.owner,
    )
    await make_line(
        org,
        ids["b1"],
        code="ACCOM",
        net=2000,
        description="Hotel stay",
        supplier="Premier Inn",
        payer="practice",
        incurred_on=date(2026, 9, 5),
    )
    ids["b2"] = await make_claim(
        org,
        bob,
        status="returned",
        title="Bob returned",
        submitted_at=at(10),
        decided_by=team.owner,
    )
    await make_line(org, ids["b2"], net=500, incurred_on=date(2026, 8, 20))
    ids["o1"] = await make_claim(org, team.owner)
    return team, alice, bob, ids, {"acme": acme, "beta": beta, "job": job}


async def claims(client, team, actor=None, **params) -> list[str]:
    response = await client.get(
        claim_url(team.org), headers=(actor or team.owner).headers, params={"limit": 100, **params}
    )
    return [c["id"] for c in ok_data(response)]


def names(ids: dict, *keys: str) -> list[str]:
    return [str(ids[key]) for key in keys]


async def test_the_list_shows_every_claim_newest_first_with_its_totals(client):
    team, _alice, _bob, ids, _ = await build(client)
    response = await client.get(
        claim_url(team.org), headers=team.owner.headers, params={"limit": 100}
    )
    data = ok_data(response)
    assert [c["id"] for c in data] == names(ids, "o1", "b2", "b1", "a2", "a1")
    by_id = {c["id"]: c for c in data}
    a2 = by_id[str(ids["a2"])]
    assert a2["totals"] == {
        "net": 3000,
        "vat": 600,
        "gross": 3600,
        "recharge": 3300,
        "reimbursable": 3600,
    }
    assert (a2["line_count"], a2["first_date"], a2["last_date"]) == (1, "2026-09-10", "2026-09-10")
    assert a2["user_name"] == "Alice Adams" and a2["status"] == "submitted"
    assert a2["submitted_at"].startswith("2026-09-12T12:00")
    assert by_id[str(ids["b1"])]["totals"]["reimbursable"] == 0  # the practice paid
    assert by_id[str(ids["b1"])]["decided_by_name"] == team.owner.full_name
    assert by_id[str(ids["o1"])]["totals"] == {
        "net": 0,
        "vat": 0,
        "gross": 0,
        "recharge": 0,
        "reimbursable": 0,
    }
    assert response.json()["meta"] == {"next_cursor": None, "has_more": False}


async def test_the_list_filters(client):
    team, alice, bob, ids, refs = await build(client)
    assert set(await claims(client, team, status=["draft", "returned"])) == set(
        names(ids, "a1", "o1", "b2")
    )
    assert await claims(client, team, status="approved") == names(ids, "b1")
    assert set(await claims(client, team, user_id=str(alice.id))) == set(names(ids, "a1", "a2"))
    assert await claims(client, team, user_id=str(team.owner.id)) == names(ids, "o1")
    # Dates, clients and jobs match a line of the claim (inclusive).
    assert set(await claims(client, team, date_from="2026-09-05", date_to="2026-09-10")) == set(
        names(ids, "a2", "b1")
    )
    assert await claims(client, team, date_from="2026-09-10") == names(ids, "a2")
    assert await claims(client, team, date_to="2026-08-31") == names(ids, "b2")
    assert await claims(client, team, client_id=str(refs["acme"].id)) == names(ids, "a1")
    assert await claims(client, team, client_id=str(refs["beta"].id)) == names(ids, "a2")
    assert await claims(client, team, job_id=str(refs["job"].id)) == names(ids, "a2")
    # The same line must match them all.
    assert await claims(client, team, client_id=str(refs["acme"].id), date_from="2026-09-05") == []
    # Search: a line's description or supplier, a title, a claimant's name.
    assert await claims(client, team, q="hotel") == names(ids, "b1")
    assert await claims(client, team, q="PREMIER") == names(ids, "b1")
    assert await claims(client, team, q="bob returned") == names(ids, "b2")
    assert set(await claims(client, team, q="alice")) == set(names(ids, "a1", "a2"))
    assert await claims(client, team, q="no such thing") == []
    assert set(await claims(client, team, q="  ")) == set(names(ids, "o1", "b2", "b1", "a2", "a1"))
    # Mine.
    assert set(await claims(client, team, alice, mine="true")) == set(names(ids, "a1", "a2"))
    assert await claims(client, team, mine="true") == names(ids, "o1")
    assert await claims(client, team, alice, mine="true", user_id=str(bob.id)) == []
    for params in (
        {"status": "paid"},
        {"date_from": "2026-09-10", "date_to": "2026-09-01"},
        {"date_from": "yesterday"},
        {"client_id": "x"},
        {"mine": "perhaps"},
        {"sort": "title"},
        {"order": "up"},
        {"limit": 0},
        {"limit": 101},
        {"q": "x" * 201},
    ):
        response = await client.get(claim_url(team.org), headers=team.owner.headers, params=params)
        assert response.status_code == 422, params


async def test_the_list_sorts_and_every_sort_pages_to_the_same_order(client):
    team, _alice, _bob, ids, _ = await build(client)
    expected = {
        ("created_at", None): names(ids, "o1", "b2", "b1", "a2", "a1"),
        ("created_at", "asc"): names(ids, "a1", "a2", "b1", "b2", "o1"),
        ("submitted_at", None): names(ids, "a2", "b1", "b2", "o1", "a1"),  # drafts last
        ("submitted_at", "asc"): names(ids, "a1", "o1", "b2", "b1", "a2"),
        ("total", None): names(ids, "a2", "b1", "a1", "b2", "o1"),
        ("total", "asc"): names(ids, "o1", "b2", "a1", "b1", "a2"),
        ("user", None): names(ids, "a1", "a2", "b1", "b2", "o1"),  # Alice, Bob, then the Owner
        ("user", "desc"): names(ids, "o1", "b2", "b1", "a2", "a1"),
    }
    for (sort, order), want in expected.items():
        params = {"sort": sort, **({"order": order} if order else {})}
        assert await claims(client, team, **params) == want, (sort, order)
        # Page by page (two a time) gives the same list.
        seen, cursor = [], None
        for _ in range(5):
            page = {**params, "limit": 2, **({"cursor": cursor} if cursor else {})}
            response = await client.get(
                claim_url(team.org), headers=team.owner.headers, params=page
            )
            seen += [c["id"] for c in ok_data(response)]
            cursor = response.json()["meta"]["next_cursor"]
            if not cursor:
                break
        assert seen == want, (sort, order)
    # A cursor belongs to the sort and the direction it came from.
    first = await client.get(
        claim_url(team.org), headers=team.owner.headers, params={"sort": "total", "limit": 2}
    )
    cursor = first.json()["meta"]["next_cursor"]
    assert first.json()["meta"]["has_more"] is True
    for params in (
        {"cursor": cursor},
        {"cursor": cursor, "sort": "user"},
        {"cursor": cursor, "sort": "total", "order": "asc"},
        {"cursor": "bogus", "sort": "total"},
    ):
        response = await client.get(claim_url(team.org), headers=team.owner.headers, params=params)
        assert err(response, 422)["details"][0]["field"] == "cursor", params


async def test_the_list_shows_only_what_the_caller_may_see(client):
    team, alice, bob, ids, _ = await build(client)
    assert set(await claims(client, team, alice)) == set(names(ids, "a1", "a2"))
    assert set(await claims(client, team, bob)) == set(names(ids, "b1", "b2"))
    assert set(await claims(client, team, alice, user_id=str(bob.id))) == set()
    assert len(await claims(client, team, await team.member("billing"))) == 5
    assert len(await claims(client, team, await team.member("manager"))) == 5
    # The totals of a visible claim are the same whoever looks.
    mine = await client.get(claim_url(team.org), headers=alice.headers, params={"sort": "total"})
    assert [c["totals"]["gross"] for c in ok_data(mine)] == [3600, 1000]


async def test_the_summary_counts_the_visible_claims_and_the_approval_queue(client):
    team, alice, _bob, _ids, _ = await build(client)
    summary = ok_data(
        await client.get(claim_url(team.org, tail="/summary"), headers=team.owner.headers)
    )
    assert summary == {
        "counts": {"draft": 2, "submitted": 1, "approved": 1, "returned": 1, "total": 5},
        "pending_gross": 3600,
        "awaiting_my_approval": {"count": 1, "gross": 3600},
    }
    mine = ok_data(await client.get(claim_url(team.org, tail="/summary"), headers=alice.headers))
    assert mine == {
        "counts": {"draft": 1, "submitted": 1, "approved": 0, "returned": 0, "total": 2},
        "pending_gross": 3600,
        "awaiting_my_approval": {"count": 0, "gross": 0},  # no expense.approve
    }
    billing = await team.member("billing")
    seen = ok_data(await client.get(claim_url(team.org, tail="/summary"), headers=billing.headers))
    assert seen["counts"]["total"] == 5 and seen["pending_gross"] == 3600
    assert seen["awaiting_my_approval"] == {"count": 0, "gross": 0}
    # An approver's queue is the submitted claims of the people they manage.
    await add_custom_role(
        team.org["id"], "Approver", ["organization.read", "expense.log", "expense.approve"]
    )
    approver = await add_member(team.org["id"], "Approver")
    await set_manager(team.org, alice, approver)
    queue = ok_data(
        await client.get(claim_url(team.org, tail="/summary"), headers=approver.headers)
    )
    assert queue["counts"]["total"] == 2  # her own list: Alice's two claims
    assert queue["awaiting_my_approval"] == {"count": 1, "gross": 3600}
    # Nothing to approve for a person without the permission, and a viewer has no summary.
    viewer = await team.member("viewer")
    refused = await client.get(claim_url(team.org, tail="/summary"), headers=viewer.headers)
    assert error(refused)["code"] == "PERMISSION_DENIED"


# --- The line list --------------------------------------------------------------------------


async def lines(client, team, actor=None, **params) -> list[dict]:
    response = await client.get(
        team.url("/expenses"),
        headers=(actor or team.owner).headers,
        params={"limit": 100, **params},
    )
    return ok_data(response)


async def test_the_line_list_carries_the_claim_and_filters_by_the_line(client):
    team, alice, bob, ids, refs = await build(client)
    everything = await lines(client, team)
    assert len(everything) == 4
    # Newest line date first by default.
    assert [line["incurred_on"] for line in everything] == [
        "2026-09-10",
        "2026-09-05",
        "2026-09-01",
        "2026-08-20",
    ]
    printing = everything[0]
    assert printing["claim_id"] == str(ids["a2"]) and printing["claim_status"] == "submitted"
    assert printing["claim_title"] == "Alice submitted" and printing["user_name"] == "Alice Adams"
    assert printing["user_id"] == str(alice.id) and printing["gross"] == 3600
    assert printing["client"]["name"] == "Beta Ltd" and printing["job"]["name"] == "Accounts"
    assert printing["expense_type"]["code"] == "PRINTING" and printing["is_billable"] is True

    def got(items):
        return [line["description"] for line in items]

    assert len(await lines(client, team, status="submitted")) == 1
    assert len(await lines(client, team, status=["approved", "returned"])) == 2
    assert len(await lines(client, team, claim_id=str(ids["b1"]))) == 1
    assert len(await lines(client, team, user_id=str(bob.id))) == 2
    assert len(await lines(client, team, client_id=str(refs["acme"].id))) == 1
    assert len(await lines(client, team, job_id=str(refs["job"].id))) == 1
    assert (
        len(await lines(client, team, expense_type_id=str(await type_id(team.org, "ACCOM")))) == 1
    )
    assert len(await lines(client, team, is_billable="true")) == 1
    assert len(await lines(client, team, is_billable="false")) == 3
    assert len(await lines(client, team, billing_status="unbilled")) == 4
    assert await lines(client, team, billing_status=["billed", "written_off"]) == []
    assert len(await lines(client, team, date_from="2026-09-01", date_to="2026-09-05")) == 2
    assert got(await lines(client, team, q="hotel")) == ["Hotel stay"]
    assert len(await lines(client, team, q="alice")) == 2  # the claimant's name
    assert len(await lines(client, team, q="Bob returned")) == 1  # the claim's title
    assert [line["gross"] for line in await lines(client, team, sort="gross")] == [
        3600,
        2000,
        1000,
        500,
    ]
    assert [line["gross"] for line in await lines(client, team, sort="gross", order="asc")] == [
        500,
        1000,
        2000,
        3600,
    ]
    assert len(await lines(client, team, sort="created_at")) == 4
    # Own lines only, and only what is visible.
    assert len(await lines(client, team, alice, mine="true")) == 2
    assert len(await lines(client, team, alice)) == 2
    assert await lines(client, team, alice, user_id=str(bob.id)) == []
    assert await lines(client, team, mine="true") == []  # the Owner has a claim without lines
    for params in (
        {"status": "paid"},
        {"billing_status": "paid"},
        {"sort": "net"},
        {"is_billable": "x"},
    ):
        response = await client.get(
            team.url("/expenses"), headers=team.owner.headers, params=params
        )
        assert response.status_code == 422, params

    # Pages of two, with the cursor of the sort in use.
    seen, cursor = [], None
    for _ in range(4):
        page = {"limit": 2, "sort": "gross", **({"cursor": cursor} if cursor else {})}
        response = await client.get(team.url("/expenses"), headers=team.owner.headers, params=page)
        seen += [line["gross"] for line in ok_data(response)]
        cursor = response.json()["meta"]["next_cursor"]
        if not cursor:
            break
    assert seen == [3600, 2000, 1000, 500]
    first = await client.get(
        team.url("/expenses"), headers=team.owner.headers, params={"limit": 2, "sort": "gross"}
    )
    mixed = await client.get(
        team.url("/expenses"),
        headers=team.owner.headers,
        params={"sort": "incurred_on", "cursor": first.json()["meta"]["next_cursor"]},
    )
    assert err(mixed, 422)["details"][0]["field"] == "cursor"


async def test_the_claim_list_filters_on_a_billable_line(client):
    team, _alice, _bob, ids, refs = await build(client)
    beta, acme = str(refs["beta"].id), str(refs["acme"].id)
    assert await claims(client, team, is_billable="true") == names(ids, "a2")
    assert set(await claims(client, team, is_billable="false")) == set(names(ids, "a1", "b1", "b2"))
    # The same line must be billable and satisfy the other line filters.
    assert await claims(client, team, is_billable="true", client_id=beta) == names(ids, "a2")
    assert await claims(client, team, is_billable="false", client_id=beta) == []
    assert await claims(client, team, is_billable="true", client_id=acme) == []
    refused = await client.get(
        claim_url(team.org), headers=team.owner.headers, params={"is_billable": "x"}
    )
    assert refused.status_code == 422


async def test_the_line_list_finds_the_lines_that_still_need_a_receipt(client):
    team, alice, _bob, ids, _refs = await build(client)
    org = team.org
    taxi = await make_line(org, ids["a1"], net=3000, description="Taxi")
    await make_line(
        org,
        ids["a1"],
        code="MILEAGE",
        net=9000,
        miles=Decimal("200.00"),
        mileage_rate_applied=45,
        description="Drive",
    )
    # The printing line (36.00) and the taxi (30.00) are above the 25.00 threshold, no receipt.
    needing = await lines(client, team, needs_receipt="true")
    assert sorted(line["gross"] for line in needing) == [3000, 3600]
    assert all(line["receipt_required"] and line["receipt_count"] == 0 for line in needing)
    assert len(await lines(client, team, needs_receipt="false")) == 6
    # A receipt takes the line off the list; the other filters still apply.
    assert (await upload_receipt(client, alice, org, taxi)).status_code == 201
    assert [line["gross"] for line in await lines(client, team, needs_receipt="true")] == [3600]
    assert await lines(client, team, needs_receipt="true", claim_id=str(ids["a1"])) == []
    # Only what the caller may see.
    assert len(await lines(client, team, alice, needs_receipt="true")) == 1
    # It follows the practice threshold, and mileage never needs a receipt.
    await set_org(org, receipt_required_threshold=3600)
    assert await lines(client, team, needs_receipt="true") == []
    await set_org(org, receipt_required_threshold=0)
    wanted = {line["description"] for line in await lines(client, team, needs_receipt="true")}
    assert len(wanted) == 4 and not wanted & {"Taxi", "Drive"}
    # The status filter is the claim's: only the draft claim's line.
    assert len(await lines(client, team, needs_receipt="true", status="draft")) == 1

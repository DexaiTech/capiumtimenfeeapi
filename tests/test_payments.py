"""Payments: recording, allocation rules, auto-allocation, unallocation, voids and races."""

import asyncio

from tests.billing_helpers import count_rows, draft, get_invoice, issued, new_practice, org_url, pay
from tests.domain_factories import make_client, make_time_entry, staff
from tests.factories import audit_entries, error


async def issued_invoice(client, p, client_row=None, minutes=60, **issue_body):
    """An issued invoice for 60 minutes at £120: £144.00 gross."""
    client_row = client_row or p.client
    entry = await make_time_entry(p.org, p.owner, client=client_row, minutes=minutes)
    invoice = await draft(client, p.owner, p.org, client_row.id, time=[entry])
    return await issued(client, p.owner, p.org, invoice["id"], **issue_body)


async def test_record_and_allocate(client):
    p = await new_practice(client)
    first = await issued_invoice(client, p)
    second = await issued_invoice(client, p)
    response = await pay(
        client,
        p.owner,
        p.org,
        p.client.id,
        20_000,
        method="card",
        reference="BACS 991",
        allocations=[
            {"invoice_id": first["id"], "amount": 14_400},
            {"invoice_id": second["id"], "amount": 5_000},
        ],
    )
    assert response.status_code == 201, response.text
    payment = response.json()["data"]
    assert (payment["amount"], payment["allocated_total"], payment["unallocated"]) == (
        20_000,
        19_400,
        600,
    )
    assert payment["state"] == "part_allocated" and payment["method"] == "card"
    assert {a["invoice_number"]: a["amount"] for a in payment["allocations"]} == {
        first["number"]: 14_400,
        second["number"]: 5_000,
    }
    one = await get_invoice(client, p.owner, p.org, first["id"])
    two = await get_invoice(client, p.owner, p.org, second["id"])
    assert (one["balance"], one["display_status"]) == (0, "paid")
    assert (two["balance"], two["display_status"]) == (9_400, "part_paid")
    actions = [e["action"] for e in await audit_entries(p.id) if e["target_type"] == "payment"]
    assert actions == ["payment.recorded", "payment.allocated"]

    detail = await client.get(org_url(p.org, f"/payments/{payment['id']}"), headers=p.owner.headers)
    assert len(detail.json()["data"]["allocations"]) == 2

    # More of the same payment to the same invoice adds to its allocation.
    more = await client.post(
        org_url(p.org, f"/payments/{payment['id']}/allocations"),
        headers=p.owner.headers,
        json={"allocations": [{"invoice_id": second["id"], "amount": 600}]},
    )
    assert more.status_code == 200, more.text
    data = more.json()["data"]
    assert data["state"] == "allocated"
    assert {a["invoice_id"]: a["amount"] for a in data["allocations"]}[second["id"]] == 5_600


async def test_payment_on_account_then_auto_allocate_oldest_first(client):
    p = await new_practice(client)
    newest = await issued_invoice(client, p, issue_date="2026-10-03")
    oldest = await issued_invoice(client, p, issue_date="2026-09-01")
    middle = await issued_invoice(client, p, issue_date="2026-09-15")
    response = await pay(client, p.owner, p.org, p.client.id, 20_000)
    payment = response.json()["data"]
    assert (payment["state"], payment["unallocated"], payment["allocations"]) == (
        "unallocated",
        20_000,
        [],
    )
    allocated = await client.post(
        org_url(p.org, f"/payments/{payment['id']}/allocations"),
        headers=p.owner.headers,
        json={"auto_allocate": True},
    )
    assert allocated.status_code == 200, allocated.text
    amounts = {a["invoice_id"]: a["amount"] for a in allocated.json()["data"]["allocations"]}
    assert amounts == {oldest["id"]: 14_400, middle["id"]: 5_600}
    assert (await get_invoice(client, p.owner, p.org, newest["id"]))["paid_total"] == 0
    nothing = await client.post(
        org_url(p.org, f"/payments/{payment['id']}/allocations"),
        headers=p.owner.headers,
        json={"auto_allocate": True},
    )
    assert nothing.status_code == 409 and error(nothing)["code"] == "NOTHING_TO_ALLOCATE"

    # Recording with auto_allocate settles what is left, oldest first.
    response = await pay(client, p.owner, p.org, p.client.id, 50_000, auto_allocate=True)
    data = response.json()["data"]
    assert {a["invoice_id"]: a["amount"] for a in data["allocations"]} == {
        middle["id"]: 8_800,
        newest["id"]: 14_400,
    }
    assert data["unallocated"] == 50_000 - 23_200


async def test_allocation_rules(client):
    p = await new_practice(client)
    invoice = await issued_invoice(client, p)
    other_client = await make_client(p.org, "Other Ltd")
    theirs = await issued_invoice(client, p, other_client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    a_draft = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    other_practice = await new_practice(client, "Other Practice")
    foreign = await issued_invoice(client, other_practice)

    async def attempt(amount, allocations, status):
        response = await pay(client, p.owner, p.org, p.client.id, amount, allocations=allocations)
        assert response.status_code == status, response.text
        return error(response)

    over_balance = await attempt(20_000, [{"invoice_id": invoice["id"], "amount": 14_401}], 409)
    assert over_balance["code"] == "ALLOCATION_EXCEEDS_BALANCE"
    over_payment = await attempt(10_000, [{"invoice_id": invoice["id"], "amount": 10_001}], 409)
    assert over_payment["code"] == "ALLOCATION_EXCEEDS_PAYMENT"
    mismatch = await attempt(10_000, [{"invoice_id": theirs["id"], "amount": 100}], 422)
    assert mismatch["code"] == "CLIENT_MISMATCH"
    not_issued = await attempt(10_000, [{"invoice_id": a_draft["id"], "amount": 100}], 409)
    assert not_issued["code"] == "INVOICE_NOT_ISSUED"
    await attempt(10_000, [{"invoice_id": foreign["id"], "amount": 100}], 404)
    # The failed attempts recorded nothing.
    assert await count_rows("SELECT count(*) FROM payments") == 0

    for bad in (
        {"amount": 0},
        {"amount": -1},
        {"amount": 100, "method": "bitcoin"},
        {
            "amount": 100,
            "auto_allocate": True,
            "allocations": [{"invoice_id": invoice["id"], "amount": 1}],
        },
        {
            "amount": 100,
            "allocations": [
                {"invoice_id": invoice["id"], "amount": 1},
                {"invoice_id": invoice["id"], "amount": 2},
            ],
        },
        {"amount": 100, "allocations": [{"invoice_id": invoice["id"], "amount": 0}]},
        {"amount": 100, "surprise": 1},
    ):
        response = await pay(client, p.owner, p.org, p.client.id, **bad)
        assert response.status_code == 422, bad
    unknown_client = await pay(client, p.owner, p.org, other_practice.client.id, 100)
    assert unknown_client.status_code == 404
    payment = (await pay(client, p.owner, p.org, p.client.id, 100)).json()["data"]
    neither = await client.post(
        org_url(p.org, f"/payments/{payment['id']}/allocations"), headers=p.owner.headers, json={}
    )
    assert neither.status_code == 422


async def test_unallocate_and_void(client):
    p = await new_practice(client)
    invoice = await issued_invoice(client, p)
    payment = (
        await pay(
            client,
            p.owner,
            p.org,
            p.client.id,
            14_400,
            allocations=[{"invoice_id": invoice["id"], "amount": 14_400}],
        )
    ).json()["data"]
    void_url = org_url(p.org, f"/payments/{payment['id']}/void")
    refused = await client.post(void_url, headers=p.owner.headers, json={"reason": "Bounced"})
    assert refused.status_code == 409 and error(refused)["code"] == "PAYMENT_ALLOCATED"

    allocation_id = payment["allocations"][0]["id"]
    url = org_url(p.org, f"/payments/{payment['id']}/allocations/{allocation_id}")
    removed = await client.delete(url, headers=p.owner.headers)
    assert removed.status_code == 200, removed.text
    assert removed.json()["data"]["allocated_total"] == 0
    assert (await get_invoice(client, p.owner, p.org, invoice["id"]))["balance"] == 14_400
    assert (await client.delete(url, headers=p.owner.headers)).status_code == 404

    voided = await client.post(void_url, headers=p.owner.headers, json={"reason": "Bounced"})
    assert voided.status_code == 200
    data = voided.json()["data"]
    assert (data["state"], data["unallocated"]) == ("void", 0)
    assert data["voided_at"] is not None
    again = await client.post(void_url, headers=p.owner.headers)
    assert again.status_code == 409 and error(again)["code"] == "PAYMENT_VOID"
    allocate = await client.post(
        org_url(p.org, f"/payments/{payment['id']}/allocations"),
        headers=p.owner.headers,
        json={"allocations": [{"invoice_id": invoice["id"], "amount": 1}]},
    )
    assert allocate.status_code == 409 and error(allocate)["code"] == "PAYMENT_VOID"
    [log] = await audit_entries(p.id, "payment.voided")
    assert log["metadata"]["reason"] == "Bounced"
    assert [e["action"] for e in await audit_entries(p.id, "payment.unallocated")] == [
        "payment.unallocated"
    ]


async def test_parallel_payments_cannot_over_allocate_an_invoice(client):
    p = await new_practice(client)
    invoice = await issued_invoice(client, p)  # £144.00
    payments = [
        (await pay(client, p.owner, p.org, p.client.id, 10_000)).json()["data"] for _ in range(3)
    ]

    async def allocate(payment):
        return await client.post(
            org_url(p.org, f"/payments/{payment['id']}/allocations"),
            headers=p.owner.headers,
            json={"allocations": [{"invoice_id": invoice["id"], "amount": 10_000}]},
        )

    results = await asyncio.gather(*(allocate(x) for x in payments))
    assert sorted(r.status_code for r in results) == [200, 409, 409]
    assert {error(r)["code"] for r in results if r.status_code == 409} == {
        "ALLOCATION_EXCEEDS_BALANCE"
    }
    after = await get_invoice(client, p.owner, p.org, invoice["id"])
    assert (after["paid_total"], after["balance"]) == (10_000, 4_400)


async def test_parallel_allocations_cannot_over_spend_a_payment(client):
    p = await new_practice(client)
    invoices = [await issued_invoice(client, p) for _ in range(3)]
    payment = (await pay(client, p.owner, p.org, p.client.id, 20_000)).json()["data"]

    async def allocate(invoice):
        return await client.post(
            org_url(p.org, f"/payments/{payment['id']}/allocations"),
            headers=p.owner.headers,
            json={"allocations": [{"invoice_id": invoice["id"], "amount": 8_000}]},
        )

    results = await asyncio.gather(*(allocate(i) for i in invoices))
    assert sorted(r.status_code for r in results) == [200, 200, 409]
    assert {error(r)["code"] for r in results if r.status_code == 409} == {
        "ALLOCATION_EXCEEDS_PAYMENT"
    }
    detail = await client.get(org_url(p.org, f"/payments/{payment['id']}"), headers=p.owner.headers)
    assert detail.json()["data"]["allocated_total"] == 16_000
    paid = [(await get_invoice(client, p.owner, p.org, i["id"]))["paid_total"] for i in invoices]
    assert sorted(paid) == [0, 8_000, 8_000]


async def test_list_filters(client):
    p = await new_practice(client)
    other = await make_client(p.org, "Other Ltd")
    invoice = await issued_invoice(client, p)
    allocated = (
        await pay(
            client,
            p.owner,
            p.org,
            p.client.id,
            14_400,
            allocations=[{"invoice_id": invoice["id"], "amount": 14_400}],
        )
    ).json()["data"]
    on_account = (
        await pay(client, p.owner, p.org, p.client.id, 500, method="cheque", reference="CHQ 1")
    ).json()["data"]
    elsewhere = (await pay(client, p.owner, p.org, other.id, 700)).json()["data"]
    url = org_url(p.org, "/payments")

    async def ids(**params):
        response = await client.get(url, headers=p.owner.headers, params=params)
        assert response.status_code == 200, response.text
        return {x["id"] for x in response.json()["data"]}

    assert await ids() == {allocated["id"], on_account["id"], elsewhere["id"]}
    assert await ids(client_id=str(other.id)) == {elsewhere["id"]}
    assert await ids(state="allocated") == {allocated["id"]}
    assert await ids(state="on_account") == {on_account["id"], elsewhere["id"]}
    assert await ids(method="cheque") == {on_account["id"]}
    assert await ids(q="chq") == {on_account["id"]}
    assert await ids(date_from="2026-10-02") == set()
    page = (await client.get(url, headers=p.owner.headers, params={"limit": 2})).json()
    rest = await client.get(
        url, headers=p.owner.headers, params={"limit": 2, "cursor": page["meta"]["next_cursor"]}
    )
    assert len(page["data"]) + len(rest.json()["data"]) == 3


async def test_permissions_and_tenancy(client):
    p = await new_practice(client)
    invoice = await issued_invoice(client, p)
    viewer = await staff(p.org, "viewer")
    manager = await staff(p.org, "manager")
    billing = await staff(p.org, "billing")
    staff_member = await staff(p.org, "staff")
    for denied in (viewer, manager):
        response = await pay(client, denied, p.org, p.client.id, 100)
        assert response.status_code == 403
    assert (
        await client.get(org_url(p.org, "/payments"), headers=staff_member.headers)
    ).status_code == 403
    payment = (await pay(client, billing, p.org, p.client.id, 100)).json()["data"]
    assert (
        await client.get(org_url(p.org, "/payments"), headers=viewer.headers)
    ).status_code == 200
    allocate = await client.post(
        org_url(p.org, f"/payments/{payment['id']}/allocations"),
        headers=manager.headers,
        json={"allocations": [{"invoice_id": invoice["id"], "amount": 100}]},
    )
    assert allocate.status_code == 403

    other = await new_practice(client, "Other Practice")
    for method, path, body in (
        ("get", f"/payments/{payment['id']}", None),
        ("post", f"/payments/{payment['id']}/allocations", {"auto_allocate": True}),
        ("post", f"/payments/{payment['id']}/void", None),
        ("delete", f"/payments/{payment['id']}/allocations/{payment['id']}", None),
    ):
        response = await client.request(
            method, org_url(other.org, path), headers=other.owner.headers, json=body
        )
        assert response.status_code == 404, (method, path)

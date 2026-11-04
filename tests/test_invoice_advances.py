"""Tickets APT69 and APT75: fees billed in advance, on one line or several, with the time assigned
to the invoice later.

An advance invoice is an ordinary invoice whose manual line is marked ``is_advance``. Once it is
issued, the client's unbilled WIP is assigned to that line: the items are billed (out of WIP) and
the invoice's figures do not change. The advance is used oldest work first; work beyond it is
written off; un-assigning returns the items to WIP.
"""

import uuid
from datetime import date

from sqlalchemy import text

from app.db.session import get_sessionmaker
from tests.billing_helpers import (
    billing_status,
    count_rows,
    draft,
    get_invoice,
    hourly_job,
    issued,
    new_practice,
    org_url,
    set_org,
)
from tests.domain_factories import (
    make_client,
    make_time_entry,
    service_id,
    staff,
    vat_rate_id,
)
from tests.factories import audit_entries, error
from tests.period_lock_helpers import set_lock, today
from tests.reports_helpers import report

ADVANCE = {"description": "Fees in advance 2026/27", "net_amount": 600_000}  # 6,000.00
FIGURES = ("net_total", "vat_total", "gross_total", "paid_total", "credited_total", "balance")


async def advance_invoice(http, p, advance=ADVANCE) -> tuple[dict, str]:
    """An issued advance invoice of the practice's client and its advance line id."""
    made = await draft(http, p.owner, p.org, p.client.id, advance=advance)
    invoice = await issued(http, p.owner, p.org, made["id"], issue_date="2026-04-01")
    return invoice, invoice["lines"][0]["id"]


async def assign(http, actor, org, invoice_id, line_id, *, time=(), disbursements=(), undo=False):
    action = "unassign" if undo else "assign"
    return await http.post(
        org_url(org, f"/invoices/{invoice_id}/lines/{line_id}/{action}"),
        headers=actor.headers,
        json={
            "time_entry_ids": [str(t.id) for t in time],
            "disbursement_ids": [str(d.id) for d in disbursements],
        },
    )


async def test_time_assigned_to_an_advance_leaves_wip_and_keeps_the_invoice(client):
    p = await new_practice(client)
    job = await hourly_job(p)
    made = await draft(client, p.owner, p.org, p.client.id, advance=ADVANCE)
    [line] = made["lines"]
    assert (line["kind"], line["is_advance"], line["net_amount"]) == ("manual", True, 600_000)
    assert line["advance"] == {
        "amount": 600_000,
        "used": 0,
        "remaining": 600_000,
        "wip_value": 0,
        "written_off": 0,
        "items": [],
    }
    [created] = await audit_entries(p.id, "invoice.draft_created")
    assert created["metadata"]["advance"] == 600_000
    invoice = await issued(client, p.owner, p.org, made["id"], issue_date="2026-04-01")
    line_id = invoice["lines"][0]["id"]

    # Ten hours at 150.00 an hour, logged during the year.
    entries = [
        await make_time_entry(
            p.org,
            p.owner,
            client=p.client,
            job=job,
            entry_date=day,
            minutes=minutes,
            charge_rate=15_000,
        )
        for day, minutes in (
            (date(2026, 5, 4), 240),
            (date(2026, 6, 8), 180),
            (date(2026, 7, 6), 180),
        )
    ]
    response = await assign(client, p.owner, p.org, invoice["id"], line_id, time=entries)
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    # The invoice is what was issued: the same totals, balance and status.
    assert {k: data[k] for k in FIGURES} == {k: invoice[k] for k in FIGURES}
    assert data["display_status"] == invoice["display_status"]
    advance = data["lines"][0]["advance"]
    assert (advance["amount"], advance["used"], advance["remaining"]) == (
        600_000,
        150_000,
        450_000,
    )
    assert (advance["wip_value"], advance["written_off"]) == (150_000, 0)
    assert [i["id"] for i in advance["items"]] == [str(e.id) for e in entries]
    assert [(i["hours"], i["wip_value"], i["billed_value"]) for i in advance["items"]] == [
        ("4.00", 60_000, 60_000),
        ("3.00", 45_000, 45_000),
        ("3.00", 45_000, 45_000),
    ]
    assert {i["assigned_on"] for i in advance["items"]} == {today(p.org).isoformat()}
    # The work shows under "Billed from WIP" of the invoice.
    assert data["wip"] == {"wip_value": 150_000, "billed_value": 150_000, "adjustment": 0}
    assert sorted(data["lines"][0]["wip"]["time_entry_ids"]) == sorted(str(e.id) for e in entries)

    # Billed: out of WIP.
    statuses = await billing_status("time_entries", [e.id for e in entries])
    assert set(statuses.values()) == {"billed"}
    wip = await client.get(org_url(p.org, f"/clients/{p.client.id}/wip"), headers=p.owner.headers)
    assert wip.status_code == 200
    assert not {i["id"] for i in wip.json()["data"]["items"]} & {str(e.id) for e in entries}

    [entry] = await audit_entries(p.id, "invoice.time_assigned")
    assert entry["target_id"] == invoice["id"]
    meta = entry["metadata"]
    assert (meta["item_count"], meta["wip_value"], meta["billed_value"]) == (3, 150_000, 150_000)
    assert (meta["written_off"], meta["remaining"], meta["line_id"]) == (0, 450_000, line_id)


async def test_work_beyond_the_advance_is_written_off_oldest_first(client):
    p = await new_practice(client)
    invoice, line_id = await advance_invoice(
        client, p, {"description": "Fees in advance", "net_amount": 100_000}
    )
    first, second, third = [
        await make_time_entry(
            p.org, p.owner, client=p.client, entry_date=day, minutes=minutes, charge_rate=12_000
        )
        for day, minutes in (
            (date(2026, 5, 1), 300),
            (date(2026, 5, 2), 300),
            (date(2026, 5, 3), 150),
        )
    ]
    # Given in any order, the oldest work is covered first.
    response = await assign(
        client, p.owner, p.org, invoice["id"], line_id, time=[third, first, second]
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    advance = data["lines"][0]["advance"]
    assert [(i["wip_value"], i["billed_value"]) for i in advance["items"]] == [
        (60_000, 60_000),
        (60_000, 40_000),
        (30_000, 0),
    ]
    assert (advance["used"], advance["remaining"], advance["written_off"]) == (100_000, 0, 50_000)
    assert data["wip"]["adjustment"] == -50_000
    assert data["gross_total"] == invoice["gross_total"]

    # Once used up, more work is billed at nothing.
    later = await make_time_entry(p.org, p.owner, client=p.client, entry_date=date(2026, 6, 1))
    response = await assign(client, p.owner, p.org, invoice["id"], line_id, time=[later])
    assert response.status_code == 200, response.text
    advance = response.json()["data"]["lines"][0]["advance"]
    assert (advance["used"], advance["remaining"], advance["written_off"]) == (100_000, 0, 62_000)
    assert (await billing_status("time_entries", [later.id]))[later.id] == "billed"


async def test_only_unbilled_work_of_the_client_on_an_issued_advance(client):
    p = await new_practice(client)
    invoice, line_id = await advance_invoice(client, p)
    url_args = (client, p.owner, p.org, invoice["id"], line_id)

    other = await make_client(p.org, "Other Ltd")
    theirs = await make_time_entry(p.org, p.owner, client=other)
    response = await assign(*url_args, time=[theirs])
    assert response.status_code == 422 and error(response)["code"] == "CLIENT_MISMATCH"

    for status in ("billed", "in_draft", "written_off"):
        gone = await make_time_entry(p.org, p.owner, client=p.client, billing_status=status)
        response = await assign(*url_args, time=[gone])
        assert response.status_code == 409 and error(response)["code"] == "WIP_NOT_AVAILABLE"
        assert error(response)["details"] == [{"time_entry_id": str(gone.id), "reason": status}]

    await set_org(p.org, require_timesheet_approval=True)
    waiting = await make_time_entry(
        p.org, p.owner, client=p.client, entry_date=date(2026, 8, 3), timesheet_status="submitted"
    )
    response = await assign(*url_args, time=[waiting])
    assert response.status_code == 409
    assert error(response)["details"][0]["reason"] == "timesheet_not_approved"
    await set_org(p.org, require_timesheet_approval=False)

    fixed_job = await hourly_job(p, name="Accounts", billing_basis="fixed", fixed_fee=50_000)
    fixed = await make_time_entry(p.org, p.owner, client=p.client, job=fixed_job)
    response = await assign(*url_args, time=[fixed])
    assert response.status_code == 409
    assert error(response)["details"] == [
        {"time_entry_id": str(fixed.id), "reason": "fixed_fee_job"}
    ]

    # Today in a locked period: no WIP moves.
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    await set_lock(p.org, today(p.org))
    response = await assign(*url_args, time=[entry])
    assert response.status_code == 409 and error(response)["code"] == "PERIOD_LOCKED"
    # Work dated in a locked period is billed as on any invoice, when today is open.
    await set_lock(p.org, date(2026, 9, 30))
    response = await assign(*url_args, time=[entry])
    assert response.status_code == 200, response.text
    await set_lock(p.org, None)

    # Nothing to assign, no permission.
    response = await assign(*url_args)
    assert response.status_code == 422
    viewer = await staff(p.org, "viewer")
    another = await make_time_entry(p.org, p.owner, client=p.client)
    response = await assign(client, viewer, p.org, invoice["id"], line_id, time=[another])
    assert response.status_code == 403
    assert (await billing_status("time_entries", [another.id]))[another.id] == "unbilled"
    assert len(await audit_entries(p.id, "invoice.time_assigned")) == 1


async def test_time_goes_on_an_issued_advance_line_only(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    made = await draft(client, p.owner, p.org, p.client.id, advance=ADVANCE)
    line_id = made["lines"][0]["id"]
    response = await assign(client, p.owner, p.org, made["id"], line_id, time=[entry])
    assert response.status_code == 409 and error(response)["code"] == "INVOICE_NOT_ISSUED"

    other = await make_time_entry(p.org, p.owner, client=p.client, entry_date=date(2026, 8, 3))
    ordinary = await draft(client, p.owner, p.org, p.client.id, time=[other])
    ordinary = await issued(client, p.owner, p.org, ordinary["id"])
    response = await assign(
        client, p.owner, p.org, ordinary["id"], ordinary["lines"][0]["id"], time=[entry]
    )
    assert response.status_code == 409 and error(response)["code"] == "NOT_AN_ADVANCE"
    assert (await billing_status("time_entries", [entry.id]))[entry.id] == "unbilled"


async def test_an_advance_line_is_a_positive_manual_line_of_a_draft(client):
    p = await new_practice(client)
    response = await client.post(
        org_url(p.org, "/invoices"),
        headers=p.owner.headers,
        json={"client_id": str(p.client.id), "advance": {"description": "x", "net_amount": 0}},
    )
    assert response.status_code == 422
    blank = await draft(client, p.owner, p.org, p.client.id)
    lines_url = org_url(p.org, f"/invoices/{blank['id']}/lines")
    refused = await client.post(
        lines_url,
        headers=p.owner.headers,
        json={"description": "Fees in advance", "unit_amount": -100, "is_advance": True},
    )
    assert refused.status_code == 422 and error(refused)["code"] == "ADVANCE_NOT_POSITIVE"
    added = await client.post(
        lines_url,
        headers=p.owner.headers,
        json={"description": "Fees in advance", "unit_amount": 250_000, "is_advance": True},
    )
    assert added.status_code == 201, added.text
    [line] = added.json()["data"]["lines"]
    assert line["is_advance"] is True and line["advance"]["remaining"] == 250_000
    changed = await client.patch(
        org_url(p.org, f"/invoices/{blank['id']}/lines/{line['id']}"),
        headers=p.owner.headers,
        json={"net_amount": 0},
    )
    assert changed.status_code == 422 and error(changed)["code"] == "ADVANCE_NOT_POSITIVE"
    # An ordinary manual line is not an advance.
    plain = await client.post(
        lines_url, headers=p.owner.headers, json={"description": "Advice", "unit_amount": 5_000}
    )
    assert [(x["is_advance"], x["advance"] is None) for x in plain.json()["data"]["lines"]] == [
        (True, False),
        (False, True),
    ]


async def test_unassigning_returns_the_work_to_wip(client):
    p = await new_practice(client)
    invoice, line_id = await advance_invoice(client, p)
    keep, back = [
        await make_time_entry(p.org, p.owner, client=p.client, entry_date=day)
        for day in (date(2026, 5, 4), date(2026, 5, 5))
    ]
    response = await assign(client, p.owner, p.org, invoice["id"], line_id, time=[keep, back])
    assert response.status_code == 200, response.text

    response = await assign(client, p.owner, p.org, invoice["id"], line_id, time=[back], undo=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    advance = data["lines"][0]["advance"]
    assert [i["id"] for i in advance["items"]] == [str(keep.id)]
    assert (advance["used"], advance["remaining"]) == (12_000, 588_000)
    assert {k: data[k] for k in FIGURES} == {k: invoice[k] for k in FIGURES}
    statuses = await billing_status("time_entries", [keep.id, back.id])
    assert (statuses[keep.id], statuses[back.id]) == ("billed", "unbilled")
    [entry] = await audit_entries(p.id, "invoice.time_unassigned")
    assert entry["metadata"]["time_entry_ids"] == [str(back.id)]
    assert entry["metadata"]["remaining"] == 588_000

    # Not (or no longer) on the advance.
    again = await assign(client, p.owner, p.org, invoice["id"], line_id, time=[back], undo=True)
    assert again.status_code == 409 and error(again)["code"] == "NOT_ASSIGNED"

    # Assigned on a day that is locked since: it stays.
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "UPDATE wip_allocations SET assigned_on = DATE '2026-09-07' WHERE time_entry_id = :t"
            ),
            {"t": keep.id},
        )
        await session.commit()
    await set_lock(p.org, date(2026, 9, 30))
    locked = await assign(client, p.owner, p.org, invoice["id"], line_id, time=[keep], undo=True)
    assert locked.status_code == 409 and error(locked)["code"] == "PERIOD_LOCKED"
    await set_lock(p.org, None)

    # Voiding the advance invoice returns its assigned work to WIP, as on any invoice.
    voided = await client.post(
        org_url(p.org, f"/invoices/{invoice['id']}/void"),
        headers=p.owner.headers,
        json={"reason": "Issued in error"},
    )
    assert voided.status_code == 200, voided.text
    assert (await billing_status("time_entries", [keep.id]))[keep.id] == "unbilled"
    assert (await get_invoice(client, p.owner, p.org, invoice["id"]))["lines"][0]["advance"][
        "used"
    ] == 0


async def test_wip_as_at_a_day_counts_work_assigned_after_it(client):
    """The advance invoice is dated April; work done in September and assigned later was still
    WIP at the end of September."""
    p = await new_practice(client)
    invoice, line_id = await advance_invoice(client, p)
    entry = await make_time_entry(p.org, p.owner, client=p.client, entry_date=date(2026, 9, 7))
    response = await assign(client, p.owner, p.org, invoice["id"], line_id, time=[entry])
    assert response.status_code == 200, response.text

    month_end = date(2026, 9, 30)
    then = await report(client, p.owner, p.org, "wip", as_at=month_end)
    assert then["totals"]["time_value"] == 12_000
    historic = await report(client, p.owner, p.org, "wip", as_at=month_end, group_by="client")
    assert historic["totals"]["time_wip"] == 12_000
    now = await report(client, p.owner, p.org, "wip", as_at=today(p.org))
    assert now["totals"]["time_value"] == 0


async def test_several_advance_lines_on_one_draft(client):
    """Ticket APT75: the advance is billed on several lines, quantity x unit price each."""
    p = await new_practice(client)
    zero = await vat_rate_id(p.org, "zero")
    made = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        advance_lines=[
            {"description": "Accounts 2026/27\nYear end 31 March", "unit_amount": 300_000},
            {"description": "Payroll 2026/27", "quantity": "12", "unit_amount": 12_500},
            {
                "description": "Companies House filing",
                "quantity": "2.5",
                "unit_amount": 3_333,
                "vat_rate_id": str(zero),
            },
        ],
    )
    assert [
        (x["kind"], x["is_advance"], x["position"], x["description"]) for x in made["lines"]
    ] == [
        ("manual", True, 1, "Accounts 2026/27\nYear end 31 March"),
        ("manual", True, 2, "Payroll 2026/27"),
        ("manual", True, 3, "Companies House filing"),
    ]
    assert [
        (x["quantity"], x["unit_amount"], x["net_amount"], x["vat_rate_bp"], x["vat_amount"])
        for x in made["lines"]
    ] == [
        ("1.00", 300_000, 300_000, 2000, 60_000),
        ("12.00", 12_500, 150_000, 2000, 30_000),
        # 2.5 x 33.33 = 83.325, rounded half away from zero.
        ("2.50", 3_333, 8_333, 0, 0),
    ]
    assert [x["advance"]["remaining"] for x in made["lines"]] == [300_000, 150_000, 8_333]
    net = 300_000 + 150_000 + 8_333
    assert (made["net_total"], made["vat_total"], made["gross_total"]) == (
        net,
        90_000,
        net + 90_000,
    )
    [created] = await audit_entries(p.id, "invoice.draft_created")
    assert created["metadata"]["advance"] == net

    # Each line takes its own time once the invoice is issued.
    invoice = await issued(client, p.owner, p.org, made["id"], issue_date="2026-04-01")
    first_id, second_id, _ = (x["id"] for x in invoice["lines"])
    entry = await make_time_entry(p.org, p.owner, client=p.client, entry_date=date(2026, 5, 4))
    response = await assign(client, p.owner, p.org, invoice["id"], second_id, time=[entry])
    assert response.status_code == 200, response.text
    lines = {x["id"]: x for x in response.json()["data"]["lines"]}
    assert [i["id"] for i in lines[second_id]["advance"]["items"]] == [str(entry.id)]
    assert lines[second_id]["advance"]["remaining"] == 150_000 - 12_000
    assert lines[first_id]["advance"]["items"] == []
    assert lines[first_id]["advance"]["remaining"] == 300_000


async def test_advance_lines_are_checked_before_any_draft_is_made(client):
    p = await new_practice(client)
    line = {"description": "Accounts 2026/27", "quantity": "1", "unit_amount": 300_000}
    refused = [
        {"advance": ADVANCE, "advance_lines": [line]},
        {"advance_lines": []},
        {"advance_lines": [line, {**line, "unit_amount": 0}]},
        {"advance_lines": [line, {**line, "quantity": "0"}]},
        {"advance_lines": [{**line, "description": "  "}]},
        {"advance_lines": [{"unit_amount": 300_000}]},
    ]
    for body in refused:
        response = await client.post(
            org_url(p.org, "/invoices"),
            headers=p.owner.headers,
            json={"client_id": str(p.client.id), **body},
        )
        assert response.status_code == 422, (body, response.text)
    # 0.01 x 0.01 rounds to nothing: not an advance.
    tiny = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        expect=422,
        advance_lines=[{**line, "quantity": "0.01", "unit_amount": 1}],
    )
    assert tiny["code"] == "ADVANCE_NOT_POSITIVE"
    assert (
        await count_rows("SELECT count(*) FROM invoices WHERE client_id = :c", c=str(p.client.id))
        == 0
    )


async def test_an_advance_line_bills_a_service_with_its_vat_code(client):
    p = await new_practice(client)
    reduced = await vat_rate_id(p.org, "reduced")
    zero = await vat_rate_id(p.org, "zero")
    books = await service_id(p.org, "BOOKS")
    async with get_sessionmaker()() as session:
        await session.execute(
            text("UPDATE services SET vat_rate_id = :rate WHERE id = :id"),
            {"rate": str(reduced), "id": str(books)},
        )
        await session.commit()
    on_books = {
        "description": "Bookkeeping 2026/27",
        "unit_amount": 100_000,
        "service_id": str(books),
    }
    made = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        advance_lines=[on_books, {"description": "Advice", "unit_amount": 50_000}],
    )
    # A client without a VAT code of its own: the service's, else the practice default.
    assert [(x["service_id"], x["vat_rate_bp"], x["vat_amount"]) for x in made["lines"]] == [
        (str(books), 500, 5_000),
        (None, 2000, 10_000),
    ]
    # The client's own code comes before the service's.
    zero_client = await make_client(p.org, "Zero Ltd", default_vat_rate_id=zero)
    theirs = await draft(client, p.owner, p.org, zero_client.id, advance_lines=[on_books])
    assert (theirs["lines"][0]["service_id"], theirs["lines"][0]["vat_rate_bp"]) == (str(books), 0)

    # Only a service of the practice.
    other = await new_practice(client, "Other Accountants")
    foreign = await service_id(other.org, "BOOKS")
    for unknown in (foreign, uuid.uuid4()):
        refused = await draft(
            client,
            p.owner,
            p.org,
            p.client.id,
            expect=422,
            advance_lines=[{**on_books, "service_id": str(unknown)}],
        )
        assert [d["field"] for d in refused["details"]] == ["service_id"]
    assert (
        await count_rows("SELECT count(*) FROM invoices WHERE client_id = :c", c=str(p.client.id))
        == 1
    )


async def test_wip_and_manual_lines_are_drafted_together(client):
    """Tickets APT69 and APT75: Create invoice drafts the chosen WIP and the lines typed in the
    same grid, in one request."""
    p = await new_practice(client)
    zero = await vat_rate_id(p.org, "zero")
    entry = await make_time_entry(p.org, p.owner, client=p.client)  # 1 hour at 120.00
    made = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        time=[entry],
        manual_lines=[
            {"description": "Companies House filing", "quantity": "2", "unit_amount": 1_700},
            {
                "description": "Software licence\nTwelve months",
                "unit_amount": 3_000,
                "vat_rate_id": str(zero),
            },
        ],
    )
    lines = made["lines"]
    assert [(x["position"], x["kind"], x["is_advance"]) for x in lines] == [
        (1, "time", False),
        (2, "manual", False),
        (3, "manual", False),
    ]
    assert lines[0]["net_amount"] == 12_000
    assert [
        (x["description"], x["quantity"], x["net_amount"], x["vat_rate_bp"], x["vat_amount"])
        for x in lines[1:]
    ] == [
        ("Companies House filing", "2.00", 3_400, 2000, 680),
        ("Software licence\nTwelve months", "1.00", 3_000, 0, 0),
    ]
    assert made["net_total"] == sum(x["net_amount"] for x in lines) == 18_400
    assert made["vat_total"] == sum(x["vat_amount"] for x in lines)
    assert made["gross_total"] == made["net_total"] + made["vat_total"]
    assert (await billing_status("time_entries", [entry.id]))[entry.id] == "in_draft"
    [created] = await audit_entries(p.id, "invoice.draft_created")
    assert "advance" not in created["metadata"]

    # The time is on this draft: no second invoice bills it, with or without manual lines.
    again = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        time=[entry],
        expect=409,
        manual_lines=[{"description": "Advice", "unit_amount": 5_000}],
    )
    assert again["code"] == "WIP_NOT_AVAILABLE"
    # A refused manual line makes no draft and holds no WIP.
    other = await make_time_entry(p.org, p.owner, client=p.client, entry_date=date(2026, 9, 8))
    refused = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        time=[other],
        expect=422,
        manual_lines=[{"description": "Fees", "unit_amount": -100, "is_advance": True}],
    )
    assert refused["code"] == "ADVANCE_NOT_POSITIVE"
    assert (await billing_status("time_entries", [other.id]))[other.id] == "unbilled"
    assert (
        await count_rows("SELECT count(*) FROM invoices WHERE client_id = :c", c=str(p.client.id))
        == 1
    )


async def test_manual_advance_lines_without_wip_take_time_later(client):
    """Ticket APT69: with no time to bill, the lines typed are fees in advance; time is assigned
    to them once the invoice is issued, and only once."""
    p = await new_practice(client)
    job = await hourly_job(p)
    made = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        manual_lines=[
            {"description": "Accounts 2026/27", "unit_amount": 100_000, "is_advance": True},
            {
                "description": "Payroll 2026/27",
                "quantity": "12",
                "unit_amount": 2_500,
                "is_advance": True,
            },
        ],
    )
    assert [(x["position"], x["is_advance"], x["net_amount"]) for x in made["lines"]] == [
        (1, True, 100_000),
        (2, True, 30_000),
    ]
    assert (made["net_total"], made["vat_total"]) == (130_000, 26_000)
    [created] = await audit_entries(p.id, "invoice.draft_created")
    assert created["metadata"]["advance"] == 130_000

    invoice = await issued(client, p.owner, p.org, made["id"], issue_date="2026-04-01")
    first_id, second_id = (x["id"] for x in invoice["lines"])
    # 10 hours at 120.00: the advance of 1,000.00 covers 1,000.00 of the 1,200.00.
    early = await make_time_entry(
        p.org, p.owner, client=p.client, job=job, entry_date=date(2026, 5, 4), minutes=300
    )
    late = await make_time_entry(
        p.org, p.owner, client=p.client, job=job, entry_date=date(2026, 6, 8), minutes=300
    )
    response = await assign(client, p.owner, p.org, invoice["id"], first_id, time=[early, late])
    assert response.status_code == 200, response.text
    lines = {x["id"]: x for x in response.json()["data"]["lines"]}
    advance = lines[first_id]["advance"]
    assert (advance["used"], advance["remaining"], advance["written_off"]) == (100_000, 0, 20_000)
    assert [i["billed_value"] for i in advance["items"]] == [60_000, 40_000]
    assert lines[second_id]["advance"]["remaining"] == 30_000

    # The same time cannot be assigned twice, to this line or another one.
    for line_id in (first_id, second_id):
        twice = await assign(client, p.owner, p.org, invoice["id"], line_id, time=[early])
        assert twice.status_code == 409 and error(twice)["code"] == "WIP_NOT_AVAILABLE"
    after = await get_invoice(client, p.owner, p.org, invoice["id"])
    assert [x["advance"]["used"] for x in after["lines"]] == [100_000, 0]
    assert len(await audit_entries(p.id, "invoice.time_assigned")) == 1


async def test_a_drafts_typed_line_becomes_fees_in_advance_and_back(client):
    """APT69: whether a line typed in is billed in advance is chosen per line, and can be
    changed on the draft: only for a line of more than zero with no time on it."""
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    made = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        time=[entry],
        manual_lines=[
            {"description": "Accounts 2026/27", "unit_amount": 250_000},
            {"description": "Loyalty discount", "unit_amount": -1_000},
        ],
    )
    work, fee, discount = made["lines"]
    assert fee["is_advance"] is False

    def line_url(line: dict) -> str:
        return org_url(p.org, f"/invoices/{made['id']}/lines/{line['id']}")

    marked = await client.patch(line_url(fee), headers=p.owner.headers, json={"is_advance": True})
    assert marked.status_code == 200, marked.text
    line = marked.json()["data"]["lines"][1]
    assert line["is_advance"] is True and line["advance"]["remaining"] == 250_000
    [changed] = await audit_entries(p.id, "invoice.line_updated")
    assert (changed["metadata"]["old"]["is_advance"], changed["metadata"]["new"]["is_advance"]) == (
        False,
        True,
    )
    unmarked = await client.patch(
        line_url(fee), headers=p.owner.headers, json={"is_advance": False}
    )
    line = unmarked.json()["data"]["lines"][1]
    assert line["is_advance"] is False and line["advance"] is None

    # A discount is not fees in advance, nor is a line of work.
    refused = await client.patch(
        line_url(discount), headers=p.owner.headers, json={"is_advance": True}
    )
    assert refused.status_code == 422 and error(refused)["code"] == "ADVANCE_NOT_POSITIVE"
    work_line = await client.patch(
        line_url(work), headers=p.owner.headers, json={"is_advance": True}
    )
    assert work_line.status_code == 422 and error(work_line)["code"] == "ADVANCE_NOT_MANUAL"
    nulled = await client.patch(line_url(fee), headers=p.owner.headers, json={"is_advance": None})
    assert nulled.status_code == 422

    # Saving the grid in one request takes it too.
    saved = await client.put(
        org_url(p.org, f"/invoices/{made['id']}/lines"),
        headers=p.owner.headers,
        json={
            "lines": [
                {"id": work["id"]},
                {"id": fee["id"], "is_advance": True},
                {"id": discount["id"]},
            ]
        },
    )
    assert saved.status_code == 200, saved.text
    assert [x["is_advance"] for x in saved.json()["data"]["lines"]] == [False, True, False]

    # A line with time on it keeps what it is (the work's allocation moved onto the fee).
    async with get_sessionmaker()() as session:
        await session.execute(
            text("UPDATE wip_allocations SET invoice_line_id = :fee WHERE invoice_id = :invoice"),
            {"fee": fee["id"], "invoice": made["id"]},
        )
        await session.commit()
    has_time = await client.patch(
        line_url(fee), headers=p.owner.headers, json={"is_advance": False}
    )
    assert has_time.status_code == 409 and error(has_time)["code"] == "ADVANCE_HAS_TIME"
    assert (await get_invoice(client, p.owner, p.org, made["id"]))["lines"][1]["is_advance"] is True


async def test_taking_time_off_a_used_up_advance_covers_the_later_work_again(client):
    """APT69: the advance is used oldest work first, after an un-assign too: what it no longer
    covers goes to the work after it, which was written off."""
    p = await new_practice(client)
    invoice, line_id = await advance_invoice(
        client, p, {"description": "Fees in advance", "net_amount": 100_000}
    )
    first, second, third = [
        await make_time_entry(
            p.org, p.owner, client=p.client, entry_date=day, minutes=minutes, charge_rate=12_000
        )
        for day, minutes in (
            (date(2026, 5, 1), 300),
            (date(2026, 5, 2), 300),
            (date(2026, 5, 3), 150),
        )
    ]
    response = await assign(
        client, p.owner, p.org, invoice["id"], line_id, time=[first, second, third]
    )
    assert response.status_code == 200, response.text
    advance = response.json()["data"]["lines"][0]["advance"]
    assert (advance["used"], advance["remaining"], advance["written_off"]) == (100_000, 0, 50_000)

    response = await assign(client, p.owner, p.org, invoice["id"], line_id, time=[first], undo=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    advance = data["lines"][0]["advance"]
    assert [(i["id"], i["wip_value"], i["billed_value"]) for i in advance["items"]] == [
        (str(second.id), 60_000, 60_000),
        (str(third.id), 30_000, 30_000),
    ]
    assert (advance["used"], advance["remaining"], advance["written_off"]) == (90_000, 10_000, 0)
    assert data["wip"]["adjustment"] == 0
    assert {k: data[k] for k in FIGURES} == {k: invoice[k] for k in FIGURES}
    [entry] = await audit_entries(p.id, "invoice.time_unassigned")
    assert entry["metadata"]["remaining"] == 10_000
    assert entry["metadata"]["rebilled"] == {
        str(second.id): [40_000, 60_000],
        str(third.id): [0, 30_000],
    }


async def test_writing_work_off_on_an_advance_needs_wip_write_off(client):
    """APT69: work the advance does not cover is written off, which takes the permission to
    write off WIP as well as the one to prepare invoices."""
    p = await new_practice(client)
    invoice, line_id = await advance_invoice(
        client, p, {"description": "Fees in advance", "net_amount": 100_000}
    )
    manager = await staff(p.org, "manager")
    covered, beyond = [
        await make_time_entry(
            p.org, p.owner, client=p.client, entry_date=day, minutes=300, charge_rate=12_000
        )
        for day in (date(2026, 5, 1), date(2026, 5, 2))
    ]
    # Within the advance: no write-off, preparing invoices is enough.
    response = await assign(client, manager, p.org, invoice["id"], line_id, time=[covered])
    assert response.status_code == 200, response.text

    refused = await assign(client, manager, p.org, invoice["id"], line_id, time=[beyond])
    assert refused.status_code == 403, refused.text
    assert "write off" in error(refused)["message"]
    assert (await billing_status("time_entries", [beyond.id]))[beyond.id] == "unbilled"
    assert len(await audit_entries(p.id, "invoice.time_assigned")) == 1

    allowed = await assign(client, p.owner, p.org, invoice["id"], line_id, time=[beyond])
    assert allowed.status_code == 200, allowed.text
    advance = allowed.json()["data"]["lines"][0]["advance"]
    assert (advance["used"], advance["written_off"]) == (100_000, 20_000)

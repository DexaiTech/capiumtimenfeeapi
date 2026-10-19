"""Helpers for the tests of the dashboard pages: the pinned clock, the practice's people and the
time, WIP and billing data the cards and widgets are worked out from.

Today is Saturday 17 October 2026 (in London). The practice's week starts on Monday, so the
current week is 12-18 October and the last completed one 5-11 October. Every member starts on
14 September, so only the weeks from then are ever expected to have a timesheet.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pytest
from httpx import AsyncClient

from app.db.models import Client, Invoice
from app.services import reports
from tests.domain_factories import (
    make_client,
    make_disbursement,
    make_job,
    make_time_entry,
    make_timesheet,
    practice,
    staff,
)
from tests.expense_helpers import make_claim, make_line
from tests.factories import API, Actor
from tests.reports_helpers import (
    LineSpec,
    MadeInvoice,
    credit_note,
    make_invoice,
    payment,
    set_member,
    update_row,
)

# Saturday 17 October 2026, 00:30 in London (BST); still Friday the 16th in UTC.
NOW = datetime(2026, 10, 16, 23, 30, tzinfo=UTC)
TODAY = date(2026, 10, 17)
START = date(2026, 9, 14)

__all__ = [
    "NOW",
    "START",
    "TODAY",
    "Cast",
    "Money",
    "cards_by_key",
    "draft_by",
    "get_page",
    "page_url",
    "pin_clock",
    "seed_approvals",
    "seed_money",
    "seed_time",
    "team",
]


def pin_clock(monkeypatch: pytest.MonkeyPatch, instant: datetime = NOW) -> None:
    """The report services and the time services both believe it is ``instant``."""
    monkeypatch.setattr(reports, "utc_now", lambda: instant)
    monkeypatch.setattr("app.services.time_rules.practice_now", lambda: instant)


def page_url(org: dict, path: str) -> str:
    return f"{API}/organizations/{org['id']}/dashboard/{path}"


async def get_page(
    client: AsyncClient, actor: Actor, org: dict, path: str, status: int = 200, **params: Any
) -> Any:
    """GET a dashboard page; the ``data`` of a 200, else the error."""
    clean = {key: str(value) for key, value in params.items() if value is not None}
    response = await client.get(page_url(org, path), headers=actor.headers, params=clean)
    assert response.status_code == status, response.text
    body = response.json()
    return body["data"] if status == 200 else body["error"]


def cards_by_key(data: dict) -> dict[str, dict]:
    return {card["key"]: card for card in data["cards"]}


@dataclass
class Cast:
    org: dict
    owner: Actor  # Owner: every permission
    sam: Actor  # Staff, managed by the owner
    olly: Actor  # Staff, managed by nobody
    mia: Actor  # Manager: time.read_all, the approve permissions, WIP and billing


async def team(client: AsyncClient) -> Cast:
    owner, org = await practice(client)
    sam = await staff(org, "staff", full_name="Sam Staff")
    olly = await staff(org, "staff", full_name="Olly Other")
    mia = await staff(org, "manager", full_name="Mia Manager")
    for person in (owner, sam, olly, mia):
        await set_member(org, person, start_date=START)
    await set_member(org, sam, manager_user_id=owner.id)
    return Cast(org, owner, sam, olly, mia)


async def seed_time(c: Cast) -> Client:
    """Timesheets of the weeks since 14 September, and the owner's time.

    Missing for the last completed week (5 October): the owner (open) and Olly (none). Week of
    28 September: Olly (rejected). Week of 21 September: Olly (none). The owner logged 100
    minutes on 15 September, 240 on 6 October, 60 on Sunday 11 October, 90 (billable) on 12
    October and 30 on 13 October.
    """
    alpha = await make_client(c.org, "Alpha Ltd", code="ALP")
    job = await make_job(c.org, alpha, "Accounts", number="JT0001", manager_user_id=c.owner.id)
    everyone = (c.owner, c.sam, c.olly, c.mia)
    for person in everyone:
        await make_timesheet(c.org, person, date(2026, 9, 14), "approved")
    for person in (c.owner, c.sam, c.mia):
        await make_timesheet(c.org, person, date(2026, 9, 21), "approved")
        await make_timesheet(c.org, person, date(2026, 9, 28), "approved")
    await make_timesheet(c.org, c.olly, date(2026, 9, 28), "rejected")
    await make_timesheet(c.org, c.sam, date(2026, 10, 5), "submitted")
    await make_timesheet(c.org, c.mia, date(2026, 10, 5), "approved")
    await make_time_entry(
        c.org, c.owner, entry_date=date(2026, 9, 15), minutes=100, timesheet_status="approved"
    )
    await make_time_entry(
        c.org, c.owner, entry_date=date(2026, 10, 6), minutes=240, timesheet_status="open"
    )
    await make_time_entry(
        c.org, c.owner, entry_date=date(2026, 10, 11), minutes=60, timesheet_status="open"
    )
    await make_time_entry(
        c.org,
        c.owner,
        client=alpha,
        job=job,
        entry_date=date(2026, 10, 12),
        minutes=90,
        timesheet_status="open",
    )
    await make_time_entry(
        c.org, c.owner, entry_date=date(2026, 10, 13), minutes=30, timesheet_status="open"
    )
    return alpha


async def seed_approvals(c: Cast) -> None:
    """Sam's timesheet of 5 October is submitted (it is in ``seed_time``); Olly's expense claim
    of 12 September is submitted: 10.00 net + 2.00 VAT."""
    claim = await make_claim(c.org, c.olly, status="submitted", title="Train to Leeds")
    await make_line(c.org, claim, net=1_000, vat=200)


@dataclass
class Money:
    alpha: Client
    beta: Client
    gamma: Client
    a: MadeInvoice  # alpha, issued 1 Oct, due 16 Oct: 1200 gross, overdue 1 day
    b: MadeInvoice  # beta, issued 16 Sep, due 17 Oct: 600 gross, due today
    c: MadeInvoice  # alpha, issued 1 Aug, due 31 Aug: 2400 gross, 400 paid, 240 credited
    d: MadeInvoice  # gamma, issued 1 Jun, due 30 Jun: 120 gross
    e: MadeInvoice  # alpha, issued 10 Oct, due 9 Nov: 600 gross


async def seed_money(c: Cast, alpha: Client) -> Money:
    """WIP, invoices, a credit note and payments.

    WIP (approval is required): Alpha 15 000 (60 minutes of 1 July, 15 of 2 October), Beta
    1 300 (a disbursement of 1 October), Gamma 6 000 (30 minutes of 13 October); 22 300 in all
    of which 12 000 is over 90 days old.

    Debtors today: A 1 200, B 600, C 1 760, D 120, E 600 = 4 280, of which 3 080 is overdue.
    Billed (net of invoices by issue date): June 100, August 2 000, September 500, October 1 500.
    """
    beta = await make_client(c.org, "Beta LLP", code="BET")
    gamma = await make_client(c.org, "Gamma plc", code="GAM")
    await make_time_entry(
        c.org,
        c.sam,
        client=alpha,
        entry_date=date(2026, 7, 1),
        minutes=60,
        timesheet_status="approved",
    )
    await make_time_entry(
        c.org,
        c.sam,
        client=alpha,
        entry_date=date(2026, 10, 2),
        minutes=15,
        timesheet_status="approved",
    )
    await make_time_entry(
        c.org,
        c.sam,
        client=gamma,
        entry_date=date(2026, 10, 13),
        minutes=30,
        timesheet_status="approved",
    )
    await make_disbursement(c.org, beta, 1_300, incurred_on=date(2026, 10, 1))

    a = await make_invoice(
        c.org, alpha, [LineSpec(1_000)], issue_date=date(2026, 10, 1), due_date=date(2026, 10, 16)
    )
    b = await make_invoice(
        c.org, beta, [LineSpec(500)], issue_date=date(2026, 9, 16), due_date=date(2026, 10, 17)
    )
    inv_c = await make_invoice(
        c.org, alpha, [LineSpec(2_000)], issue_date=date(2026, 8, 1), due_date=date(2026, 8, 31)
    )
    d = await make_invoice(
        c.org, gamma, [LineSpec(100)], issue_date=date(2026, 6, 1), due_date=date(2026, 6, 30)
    )
    e = await make_invoice(
        c.org, alpha, [LineSpec(500)], issue_date=date(2026, 10, 10), due_date=date(2026, 11, 9)
    )
    await make_invoice(c.org, beta, [LineSpec(9_999)], issue_date=date(2026, 10, 2), status="void")
    await make_invoice(c.org, beta, [LineSpec(7_777)], issue_date=None, status="draft")
    await payment(c.org, alpha, 400, received_on=date(2026, 9, 30), allocations=[(inv_c, 400)])
    await payment(c.org, beta, 1_000, received_on=date(2026, 10, 12))
    await credit_note(c.org, inv_c, [(200, 2000)], issue_date=date(2026, 10, 5))
    return Money(alpha, beta, gamma, a, b, inv_c, d, e)


async def draft_by(invoice: MadeInvoice, user: Actor) -> None:
    """Make ``user`` the person who prepared the invoice."""
    await update_row(Invoice, invoice.id, created_by=user.id)

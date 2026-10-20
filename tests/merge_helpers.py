"""Helpers for the client merge tests: give a client work in every table that refers to it."""

import uuid
from dataclasses import dataclass
from datetime import date

from httpx import AsyncClient
from sqlalchemy import select

from app.db.models import Client, StatementSend
from app.db.session import get_sessionmaker
from app.repositories.client_merge import MOVED_TABLES
from tests.billing_helpers import draft, issued, pay
from tests.clients_helpers import org_url, sql
from tests.domain_factories import (
    make_disbursement,
    make_job,
    make_time_entry,
    service_id,
)
from tests.expense_helpers import make_claim, make_line
from tests.factories import Actor
from tests.reports_helpers import make_timer
from tests.ticket_helpers import Team

__all__ = ["Work", "client_counts", "give_work", "orm_client"]


@dataclass
class Work:
    """What ``give_work`` created for one client."""

    client_id: str
    jobs: list[uuid.UUID]
    time_entries: list[uuid.UUID]
    disbursements: list[uuid.UUID]
    expenses: list[uuid.UUID]
    invoices: list[str]
    credit_note_id: str
    payments: list[str]
    contacts: list[str]
    engagements: list[str]
    rates: list[str]


async def orm_client(client_id: str | uuid.UUID) -> Client:
    async with get_sessionmaker()() as session:
        found = await session.scalar(select(Client).where(Client.id == uuid.UUID(str(client_id))))
    assert found is not None
    return found


async def client_counts(client_id: str | uuid.UUID) -> dict[str, int]:
    """Rows per table of the merge that name the client."""
    counts = {}
    for table in MOVED_TABLES:
        rows = await sql(
            f"SELECT count(*) AS n FROM {table} WHERE client_id = :c",  # noqa: S608
            c=str(client_id),
        )
        counts[table] = int(rows[0]["n"])
    return counts


async def give_work(
    http: AsyncClient,
    team: Team,
    client_id: str,
    *,
    services: tuple[str, ...] = ("VAT", "BOOKS"),
    contacts: tuple[tuple[str, bool], ...] = (("Pat Primary", False), ("Bill Payer", True)),
    rates: bool = True,
    timer_user: Actor | None = None,
) -> Work:
    """Rows in every table the merge moves: two jobs, three time entries (one billed, one
    written off), a timer, two disbursements, two expense claim lines, an issued and a draft
    invoice, a credit note, two payments (one allocated), an estimate, a recurring schedule, contacts, engagements and two charge
    rates."""
    org, owner = team.org, team.owner
    client = await orm_client(client_id)
    accounts = await make_job(org, client, "Accounts")
    tax = await make_job(org, client, "Tax return", billing_basis="fixed", fixed_fee=50_000)
    billed = await make_time_entry(
        org, owner, client=client, job=accounts, minutes=60, entry_date=date(2026, 9, 7)
    )
    written_off = await make_time_entry(
        org, owner, client=client, job=accounts, minutes=30, entry_date=date(2026, 9, 8)
    )
    open_entry = await make_time_entry(
        org, owner, client=client, job=tax, minutes=45, entry_date=date(2026, 9, 9)
    )
    # A person has one timer: give the second client's work to another person.
    await make_timer(
        org, timer_user or owner, started_at=None, accumulated_seconds=120, client=client, job=tax
    )
    billed_expense = await make_disbursement(org, client, 1_300)
    open_expense = await make_disbursement(org, client, 2_500)
    # Two lines of a draft expense claim (never work in progress), one on a job of the client.
    claim = await make_claim(org, owner)
    expense_lines = [
        await make_line(org, claim, client=client, job=accounts, incurred_on=date(2026, 9, 7)),
        await make_line(org, claim, client=client, incurred_on=date(2026, 9, 8)),
    ]

    first = await draft(http, owner, org, client.id, time=[billed])
    first = await issued(http, owner, org, first["id"])
    second = await draft(http, owner, org, client.id, disbursements=[billed_expense])
    credit = await http.post(
        org_url(org, f"/invoices/{first['id']}/credit-notes"),
        headers=owner.headers,
        json={
            "reason": "Goodwill",
            "lines": [{"description": "Fee reduction", "net_amount": 1_000}],
        },
    )
    assert credit.status_code == 201, credit.text
    allocated = await pay(
        http,
        owner,
        org,
        client.id,
        10_000,
        allocations=[{"invoice_id": first["id"], "amount": 5_000}],
    )
    assert allocated.status_code == 201, allocated.text
    on_account = await pay(http, owner, org, client.id, 2_000)
    assert on_account.status_code == 201, on_account.text
    off = await http.post(
        org_url(org, "/wip/write-offs"),
        headers=owner.headers,
        json={
            "client_id": str(client.id),
            "items": [{"time_entry_id": str(written_off.id)}],
            "reason": "Training time",
        },
    )
    assert off.status_code == 201, off.text
    write_on = await http.post(
        org_url(org, "/wip/adjustments"),
        headers=owner.headers,
        json={
            "client_id": str(client.id),
            "job_id": str(accounts.id),
            "kind": "write_on",
            "amount": 500,
            "reason": "Correction",
        },
    )
    assert write_on.status_code == 201, write_on.text
    transfer = await http.post(
        org_url(org, "/wip/transfers"),
        headers=owner.headers,
        json={
            "from_job_id": str(accounts.id),
            "to_job_id": str(tax.id),
            "component": "time",
            "amount": 500,
            "reason": "Rebalance",
        },
    )
    assert transfer.status_code == 201, transfer.text
    credit_note_wip = await http.post(
        org_url(org, f"/credit-notes/{credit.json()['data']['id']}/wip"),
        headers=owner.headers,
        json={"mode": "write_off", "reason": "Not returning work"},
    )
    assert credit_note_wip.status_code == 201, credit_note_wip.text
    # An estimate that names one of the client's jobs (the composite client + job key moves
    # with the job) and a recurring schedule.
    estimate = await http.post(
        org_url(org, "/estimates"),
        headers=owner.headers,
        json={
            "client_id": str(client.id),
            "job_id": str(accounts.id),
            "lines": [{"description": "Year-end accounts", "unit_amount": 80_000}],
        },
    )
    assert estimate.status_code == 201, estimate.text
    schedule = await http.post(
        org_url(org, "/recurring"),
        headers=owner.headers,
        json={
            "client_id": str(client.id),
            "name": "Bookkeeping retainer",
            "frequency": "monthly",
            "start_date": "2099-01-01",
            "lines": [{"description": "Monthly bookkeeping", "unit_amount": 25_000}],
        },
    )
    assert schedule.status_code == 201, schedule.text
    async with get_sessionmaker()() as session:
        session.add(
            StatementSend(
                organization_id=uuid.UUID(str(org["id"])),
                client_id=client.id,
                date_to=date(2026, 10, 1),
                recipients=["bill@merge.example"],
                closing_balance=0,
                sent_by=owner.id,
            )
        )
        await session.commit()

    contact_ids = []
    for name, billing in contacts:
        made = await http.post(
            org_url(org, f"/clients/{client_id}/contacts"),
            headers=owner.headers,
            json={
                "name": name,
                "email": f"{name.split()[0].lower()}@merge.example",
                "is_billing": billing,
            },
        )
        assert made.status_code == 201, made.text
        contact_ids.append(made.json()["data"]["id"])
    engagement_ids = []
    for code in services:
        made = await http.post(
            org_url(org, f"/clients/{client_id}/services"),
            headers=owner.headers,
            json={"service_id": str(await service_id(org, code)), "fee": 15_000},
        )
        assert made.status_code == 201, made.text
        engagement_ids.append(made.json()["data"]["id"])
    rate_ids = []
    if rates:
        for body in (
            {"kind": "charge", "client_id": client_id, "rate": 7_000},
            {
                "kind": "charge",
                "client_id": client_id,
                "service_id": str(await service_id(org, "VAT")),
                "rate": 8_200,
            },
        ):
            made = await http.post(
                org_url(org, "/rates"),
                headers=owner.headers,
                json={**body, "effective_from": "2026-01-01"},
            )
            assert made.status_code == 201, made.text
            rate_ids.append(made.json()["data"]["id"])
    return Work(
        client_id=client_id,
        jobs=[accounts.id, tax.id],
        time_entries=[billed.id, written_off.id, open_entry.id],
        disbursements=[billed_expense.id, open_expense.id],
        expenses=expense_lines,
        invoices=[first["id"], second["id"]],
        credit_note_id=credit.json()["data"]["id"],
        payments=[allocated.json()["data"]["id"], on_account.json()["data"]["id"]],
        contacts=contact_ids,
        engagements=engagement_ids,
        rates=rate_ids,
    )

"""The payment reminder schedule: days counted from the due date (before, on and after it), one
reminder for each day as it comes, until the invoice is paid."""

import uuid
from datetime import date, timedelta

import pytest

from app.db.models import Organization
from app.db.session import get_sessionmaker
from app.services.billing_common import today_for
from app.services.payment_reminders import queue_scheduled
from app.utils.reminder_schedule import effective_schedule, latest_step_reached
from tests.billing_helpers import draft, issued, new_practice, org_url, pay, set_org
from tests.clients_helpers import sql
from tests.conftest import run_jobs
from tests.domain_factories import make_disbursement
from tests.factories import error

DAY = date(2026, 6, 10)


# --- The schedule itself ---------------------------------------------------------------------


def test_without_a_schedule_it_is_the_single_day_after_the_due_date():
    assert effective_schedule([], 7) == [7]
    assert effective_schedule([14, -3, 0, 7, 7], 30) == [-3, 0, 7, 14]


def test_the_latest_day_that_has_come():
    due = DAY
    schedule = [-3, 0, 7]
    assert latest_step_reached(schedule, due, due - timedelta(days=4)) is None
    assert latest_step_reached(schedule, due, due - timedelta(days=3)) == due - timedelta(days=3)
    assert latest_step_reached(schedule, due, due + timedelta(days=3)) == due
    assert latest_step_reached(schedule, due, due + timedelta(days=60)) == due + timedelta(days=7)


# --- The settings ----------------------------------------------------------------------------


async def test_a_practice_without_a_schedule_reports_its_one_day(client):
    p = await new_practice(client)
    data = (await client.get(org_url(p.org, ""), headers=p.owner.headers)).json()["data"]
    org = data["organization"]
    assert org["payment_reminder_schedule"] == [org["payment_reminder_days_after_due"]]


async def test_the_schedule_is_saved_sorted(client):
    p = await new_practice(client)
    response = await client.patch(
        org_url(p.org, ""),
        headers=p.owner.headers,
        json={"payment_reminders_enabled": True, "payment_reminder_schedule": [14, -7, 0, 7]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["payment_reminder_schedule"] == [-7, 0, 7, 14]


@pytest.mark.parametrize("bad", [[], [0, 0], [-91], [366], list(range(13)), [1.5], ["a"]], ids=str)
async def test_a_bad_schedule_is_refused(client, bad):
    p = await new_practice(client)
    response = await client.patch(
        org_url(p.org, ""), headers=p.owner.headers, json={"payment_reminder_schedule": bad}
    )
    assert response.status_code == 422
    assert error(response)["code"] == "VALIDATION_ERROR"


# --- The daily job ---------------------------------------------------------------------------


async def today(p) -> date:
    async with get_sessionmaker()() as session:
        org = await session.get(Organization, uuid.UUID(p.id))
        assert org is not None
        return today_for(org)


async def unpaid_invoice(client, p, due: date) -> dict:
    fee = await make_disbursement(p.org, p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, disbursements=[fee])
    return await issued(
        client,
        p.owner,
        p.org,
        invoice["id"],
        issue_date=(due - timedelta(days=30)).isoformat(),
        due_date=due.isoformat(),
    )


async def run_schedule(p) -> tuple[int, int]:
    async with get_sessionmaker()() as session, session.begin():
        return await queue_scheduled(session, uuid.UUID(p.id))


async def reminders(p) -> list[dict]:
    return await sql(
        "SELECT invoice_id, template, source, status, sent_on, days_overdue FROM payment_reminders "
        "WHERE organization_id = :o ORDER BY created_at, id",
        o=p.id,
    )


async def practice_with_reminders(client, schedule: list[int] | None = None):
    p = await new_practice(client, payment_reminders_enabled=True)
    await sql(
        "UPDATE clients SET invoice_email = 'billing@widgets.example' WHERE id = :c",
        c=str(p.client.id),
    )
    if schedule is not None:
        await sql(
            "UPDATE organizations SET payment_reminder_schedule = :s WHERE id = :o",
            s=schedule,
            o=p.id,
        )
    return p


async def test_without_a_schedule_one_reminder_goes_after_the_days_after_due(client):
    p = await practice_with_reminders(client)
    await set_org(p.org, payment_reminder_days_after_due=7)
    now = await today(p)
    late = await unpaid_invoice(client, p, now - timedelta(days=8))
    await unpaid_invoice(client, p, now - timedelta(days=3))

    assert await run_schedule(p) == (1, 1)
    [row] = await reminders(p)
    assert str(row["invoice_id"]) == late["id"]
    assert (row["template"], row["source"], row["status"]) == (
        "client_payment_reminder",
        "scheduled",
        "queued",
    )
    assert row["days_overdue"] == 8
    # Run again the same day, and the day after: nothing more for the same stage.
    assert await run_schedule(p) == (0, 0)


async def test_a_reminder_goes_before_the_due_date_with_the_friendly_template(client):
    p = await practice_with_reminders(client, [-3, 0, 7])
    now = await today(p)
    soon = await unpaid_invoice(client, p, now + timedelta(days=3))
    await unpaid_invoice(client, p, now + timedelta(days=5))

    assert await run_schedule(p) == (1, 1)
    [row] = await reminders(p)
    assert str(row["invoice_id"]) == soon["id"]
    assert (row["template"], row["days_overdue"]) == ("client_payment_upcoming", 0)
    assert await run_schedule(p) == (0, 0)


async def test_each_day_of_the_schedule_reminds_once_until_the_invoice_is_paid(client):
    p = await practice_with_reminders(client, [-3, 0, 7])
    now = await today(p)
    invoice = await unpaid_invoice(client, p, now)  # due today: the -3 and 0 days have come

    assert await run_schedule(p) == (1, 1)  # one reminder, not one for each day that passed
    assert [r["template"] for r in await reminders(p)] == ["client_payment_upcoming"]

    # A week later the 7-day step has come: the reminder (overdue wording) goes.
    await sql(
        "UPDATE payment_reminders SET sent_on = :d, created_at = now() - interval '7 days' "
        "WHERE organization_id = :o",
        d=now - timedelta(days=7),
        o=p.id,
    )
    await sql(
        "UPDATE invoices SET issue_date = :s, due_date = :d WHERE id = :i",
        s=now - timedelta(days=37),
        d=now - timedelta(days=7),
        i=invoice["id"],
    )
    assert await run_schedule(p) == (1, 1)
    assert [r["template"] for r in await reminders(p)][-1] == "client_payment_reminder"
    assert await run_schedule(p) == (0, 0)

    # Paid: nothing more, whatever the schedule says.
    response = await pay(
        client, p.owner, p.org, p.client.id, invoice["gross_total"], auto_allocate=True
    )
    assert response.status_code in (200, 201), response.text
    await sql(
        "UPDATE invoices SET issue_date = :s, due_date = :d WHERE id = :i",
        s=now - timedelta(days=90),
        d=now - timedelta(days=60),
        i=invoice["id"],
    )
    assert await run_schedule(p) == (0, 0)


async def test_a_manual_reminder_counts_for_the_day_it_covers(client):
    p = await practice_with_reminders(client, [7])
    now = await today(p)
    invoice = await unpaid_invoice(client, p, now - timedelta(days=9))
    await sql(
        "INSERT INTO payment_reminders (id, organization_id, invoice_id, batch_id, template, "
        "source, status, sent_on, recipients, days_overdue, amount_due, sent_by, sent_at) "
        "SELECT gen_random_uuid(), :o, :i, gen_random_uuid(), 'client_payment_reminder', 'manual', "
        "'sent', :d, ARRAY['billing@widgets.example'], 8, 100, :u, now()",
        o=p.id,
        i=invoice["id"],
        d=now - timedelta(days=1),
        u=str(p.owner.id),
    )
    assert await run_schedule(p) == (0, 0)


async def test_the_emails_use_the_right_words(client, worker, mailbox):
    p = await practice_with_reminders(client, [-3, 7])
    now = await today(p)
    await unpaid_invoice(client, p, now + timedelta(days=3))
    await run_schedule(p)
    await run_jobs(worker)
    [message] = mailbox.to("billing@widgets.example")
    assert message["Subject"] == "Payment due soon: invoices from Acme Accountants"
    body = mailbox.text(message)
    assert "fall due for payment" in body and "in 3 days" in body

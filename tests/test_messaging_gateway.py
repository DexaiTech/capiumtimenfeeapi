"""The messaging gateway: the backend choice, the recording mock, SMS, and what a failed send
does to the job that made it."""

import pytest

from app.core.config import get_settings
from app.services.email import EmailService
from app.services.messaging import (
    Attachment,
    MockGateway,
    OutboundEmail,
    OutboundSms,
    PermanentSendError,
    SmtpGateway,
    build_gateway,
)
from tests.billing_helpers import draft, issued, new_practice
from tests.clients_helpers import scalar, sql
from tests.conftest import register, run_jobs
from tests.domain_factories import make_client, make_time_entry, practice
from tests.notification_helpers import (
    message_log,
    mock_settings,
    mock_worker,
    notify,
    put_own,
    settings_with,
)
from tests.time_helpers import org_url

NUMBER = "+447700900123"


# --- Choosing the gateway ----------------------------------------------------------------------


def test_the_backend_is_smtp_when_a_host_is_set_else_mock_unless_chosen():
    configured = get_settings()
    assert configured.smtp_host and configured.effective_messaging_backend == "smtp"
    assert isinstance(build_gateway(configured), SmtpGateway)

    unconfigured = mock_settings()
    assert unconfigured.effective_messaging_backend == "mock"
    assert isinstance(build_gateway(unconfigured), MockGateway)

    forced = settings_with(messaging_backend="mock")
    assert forced.smtp_host and forced.effective_messaging_backend == "mock"
    assert isinstance(build_gateway(forced), MockGateway)
    explicit = mock_settings(smtp_host="smtp.example.com", messaging_backend="smtp")
    assert isinstance(build_gateway(explicit), SmtpGateway)
    with pytest.raises(ValueError, match="MESSAGING_BACKEND=smtp needs SMTP_HOST"):
        mock_settings(messaging_backend="smtp")


# --- The mock records instead of sending -------------------------------------------------------


async def test_the_mock_records_an_email_and_calls_no_provider(mailbox):
    gateway = build_gateway(mock_settings())
    result = await gateway.send_email(
        OutboundEmail(
            to="ada@example.com",
            subject="Hello",
            html="<p>Hi</p>",
            text="Hi",
            template="welcome",
            attachments=[Attachment("a.pdf", b"%PDF", "application/pdf")],
        )
    )
    assert result.provider_ref and result.provider_ref.startswith("mock-")
    [row] = await message_log()
    assert (row["channel"], row["recipient"], row["subject"], row["body"]) == (
        "email",
        "ada@example.com",
        "Hello",
        "Hi",
    )
    assert (row["html_body"], row["template_key"], row["status"], row["error"]) == (
        "<p>Hi</p>",
        "welcome",
        "sent",
        None,
    )
    assert row["provider_ref"] == result.provider_ref
    assert (row["organization_id"], row["user_id"]) == (None, None)
    assert mailbox.messages == []


@pytest.mark.parametrize("settings", [mock_settings(), get_settings()], ids=["mock", "smtp"])
async def test_sms_always_goes_to_the_mock(mailbox, settings):
    result = await build_gateway(settings).send_sms(
        OutboundSms(to=NUMBER, body="Timesheet returned", template="timesheet_returned")
    )
    [row] = await message_log()
    assert (row["channel"], row["recipient"], row["body"], row["status"]) == (
        "sms",
        NUMBER,
        "Timesheet returned",
        "sent",
    )
    assert (row["subject"], row["html_body"], row["template_key"]) == (
        None,
        None,
        "timesheet_returned",
    )
    assert row["provider_ref"] == result.provider_ref
    assert mailbox.messages == []


async def test_one_time_links_are_not_kept_in_the_log(mailbox):
    await EmailService(mock_settings()).send_template(
        "verification", to="ada@example.com", name="Ada", token="SECRETTOKEN123"
    )
    [row] = await message_log()
    assert row["subject"] == "Verify your email address" and row["template_key"] == "verification"
    for stored in (row["body"], row["html_body"]):
        assert "SECRETTOKEN123" not in stored and "[removed]" in stored
    assert mailbox.messages == []


# --- Everything the worker sends goes through the gateway ---------------------------------------


async def test_the_workers_account_emails_use_the_configured_gateway(client, mailbox):
    await register(client)
    assert await run_jobs(mock_worker()) == 1
    [row] = await message_log()
    assert (row["template_key"], row["recipient"], row["status"]) == (
        "verification",
        "ada@example.com",
        "sent",
    )
    assert "token=" not in row["body"] and mailbox.messages == []
    [job] = await sql("SELECT status FROM background_jobs WHERE type = 'send_email'")
    assert job["status"] == "succeeded"


async def test_the_invoice_email_goes_through_the_gateway_too(client, mailbox):
    p = await new_practice(client)
    emailed = await make_client(p.org, "Emailed Ltd", email="bills@emailed.example")
    entry = await make_time_entry(p.org, p.owner, client=emailed)
    invoice = await draft(client, p.owner, p.org, emailed.id, time=[entry])
    await issued(client, p.owner, p.org, invoice["id"])
    sent = await client.post(
        org_url(p.org, f"/invoices/{invoice['id']}/send"), headers=p.owner.headers
    )
    assert sent.status_code == 202
    await run_jobs(mock_worker())
    [row] = await message_log(template_key="invoice")
    assert (row["recipient"], row["subject"], row["status"]) == (
        "bills@emailed.example",
        "Invoice INV-00001 from Acme Accountants",
        "sent",
    )
    assert str(row["organization_id"]) == p.org["id"]
    assert "£144.00" in row["body"] and mailbox.messages == []
    # The invoice is marked sent although nothing left the building: the mock accepted it.
    assert await scalar("SELECT sent_at IS NOT NULL FROM invoices WHERE id = :i", i=invoice["id"])


async def test_a_notification_email_is_logged_for_its_practice_and_person(client, mailbox):
    owner, org = await practice(client, "Acme Accountants")
    await notify(org, "budget_alert", [owner], merge={"job_name": "Q3"}, link="/jobs/1")
    await run_jobs(mock_worker())
    [row] = await message_log(channel="email")
    assert str(row["organization_id"]) == org["id"] and row["user_id"] == owner.id
    assert (row["template_key"], row["recipient"]) == ("budget_alert", owner.email)
    assert row["subject"].startswith("Budget alert: Q3") and mailbox.messages == []


# --- Failures ----------------------------------------------------------------------------------


async def test_a_failed_send_is_logged_with_its_error_and_retried(client, monkeypatch):
    owner, org = await practice(client)
    await notify(org, "budget_alert", [owner], title="Alert")
    worker = mock_worker()
    provider = {"up": False}

    async def call_provider(self, channel, recipient):
        if not provider["up"]:
            raise ConnectionError("provider down")
        return "mock-recovered"

    monkeypatch.setattr(MockGateway, "_call_provider", call_provider)
    assert await worker.run_once() == 1
    # The attempt is on record even though the job's own transaction rolled back.
    [failed] = await message_log()
    assert (failed["status"], failed["error"], failed["provider_ref"]) == (
        "failed",
        "ConnectionError: provider down",
        None,
    )
    [job] = await sql(
        "SELECT status, attempts, last_error, run_at > now() AS later FROM background_jobs "
        "WHERE type = 'send_notification'"
    )
    assert (job["status"], job["attempts"], job["later"]) == ("queued", 1, True)
    assert "ConnectionError" in job["last_error"]
    assert await worker.run_once() == 0  # backing off

    provider["up"] = True
    await sql("UPDATE background_jobs SET run_at = now() WHERE status = 'queued'")
    assert await worker.run_once() == 1
    assert [(r["status"], r["provider_ref"]) for r in await message_log()] == [
        ("failed", None),
        ("sent", "mock-recovered"),
    ]
    [job] = await sql(
        "SELECT status, attempts FROM background_jobs WHERE type = 'send_notification'"
    )
    assert (job["status"], job["attempts"]) == ("succeeded", 2)


async def test_a_refused_message_fails_the_job_without_a_retry(client, monkeypatch):
    owner, org = await practice(client)
    await notify(org, "budget_alert", [owner])

    async def refuse(self, channel, recipient):
        raise PermanentSendError("recipient refused")

    monkeypatch.setattr(MockGateway, "_call_provider", refuse)
    assert await mock_worker().run_once() == 1
    [row] = await message_log()
    assert (row["status"], row["error"]) == ("failed", "PermanentSendError: recipient refused")
    [job] = await sql(
        "SELECT status, attempts FROM background_jobs WHERE type = 'send_notification'"
    )
    assert (job["status"], job["attempts"]) == ("failed", 1)
    # A failed delivery does not itself raise a failed-job notification.
    assert await scalar("SELECT count(*) FROM notifications WHERE type = 'failed_job'") == 0


# --- SMS ---------------------------------------------------------------------------------------


async def test_an_sms_notification_is_sent_to_the_persons_number(client, worker, mailbox):
    owner, org = await practice(client, "Acme Accountants")
    sms_on = {"type": "budget_alert", "in_app": True, "email": False, "sms": True}
    await put_own(client, owner, org, {"preferences": [sms_on], "sms_number": "07700 900123"})
    await notify(
        org, "budget_alert", [owner], title="Budget alert: Q3", body="80% used", link="/jobs/1"
    )
    [job] = await sql("SELECT payload FROM background_jobs WHERE type = 'send_notification'")
    assert job["payload"]["channel"] == "sms" and "07700" not in str(job["payload"])
    assert await run_jobs(worker) == 1  # the default worker: SMTP for email, the mock for SMS

    [row] = await message_log()
    assert (row["channel"], row["recipient"], row["template_key"]) == (
        "sms",
        "07700900123",
        "budget_alert",
    )
    assert row["body"] == (
        f"Acme Accountants: Budget alert: Q3 - 80% used http://localhost:5173/{org['slug']}/jobs/1"
    )
    assert str(row["organization_id"]) == org["id"] and row["user_id"] == owner.id
    assert mailbox.messages == []


async def test_no_sms_is_queued_without_a_number_and_none_is_sent_to_a_removed_one(client, worker):
    owner, org = await practice(client)
    default_sms = {
        "type": "budget_alert",
        "enabled": True,
        "in_app": True,
        "email": False,
        "sms": True,
    }
    await client.put(
        org_url(org, "/notification-settings/practice"),
        headers=owner.headers,
        json={"types": [default_sms]},
    )
    assert (await notify(org, "budget_alert", [owner])).sms_queued == 0

    await put_own(client, owner, org, {"sms_number": NUMBER})
    assert (await notify(org, "budget_alert", [owner])).sms_queued == 1
    # The number is removed before the job runs: nothing is sent.
    await put_own(client, owner, org, {"sms_number": None})
    assert await run_jobs(worker) == 1
    assert await message_log() == []
    [job] = await sql(
        "SELECT status, payload FROM background_jobs WHERE type = 'send_notification'"
    )
    assert job["status"] == "succeeded" and job["payload"]["skipped"] == "no_mobile_number"

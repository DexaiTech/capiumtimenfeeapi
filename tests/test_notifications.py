"""Notifications: notify (idempotency, recipients, links), the inbox endpoints, the four triggers
that exist today and the helpers later modules call."""

import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.notification_types import CATALOGUE, NotificationType
from app.db.session import get_sessionmaker
from app.services import jobs, notification_events
from app.services.messaging import OutboundEmail, build_gateway
from app.services.notifications import NotificationService, dedupe_key
from app.workers import handlers
from tests.billing_helpers import count_rows, draft, issued, new_practice
from tests.clients_helpers import sql
from tests.conftest import Mailbox, run_jobs
from tests.domain_factories import make_client, make_time_entry, practice, staff
from tests.factories import add_member
from tests.notification_helpers import (
    inbox,
    message_log,
    mock_settings,
    notification_jobs,
    notify,
    put_own,
    put_practice,
    unread,
)
from tests.team_helpers import new_staff
from tests.time_helpers import err, log_time, ok_data, org_url, set_manager


async def act(client: AsyncClient, actor, org, sheet_id: str, action: str, json=None) -> dict:
    response = await client.post(
        org_url(org, f"/timesheets/{sheet_id}/{action}"), headers=actor.headers, json=json
    )
    return ok_data(response)


async def sheet_of(client: AsyncClient, person, org, **body) -> str:
    entry = await log_time(client, person, org, **body)
    return str(entry["timesheet_id"])


# --- The inbox ---------------------------------------------------------------------------------


async def test_the_inbox_lists_newest_first_with_a_cursor_and_an_unread_filter(client):
    owner, org = await practice(client)
    for n in range(3):
        await notify(
            org,
            "budget_alert",
            [owner],
            title=f"Alert {n}",
            body="Body",
            link=f"/jobs/{n}",
            payload={"n": n},
        )
    url = org_url(org, "/notifications")

    data = await inbox(client, owner, org)
    assert [n["title"] for n in data] == ["Alert 2", "Alert 1", "Alert 0"]
    first = data[0]
    assert first["type"] == "budget_alert" and first["body"] == "Body"
    assert (first["link"], first["payload"]) == ("/jobs/2", {"n": 2})
    assert (first["is_read"], first["read_at"], first["entity_id"]) == (False, None, None)
    assert await unread(client, owner, org) == 3

    page = (await client.get(url, headers=owner.headers, params={"limit": 2})).json()
    assert [n["title"] for n in page["data"]] == ["Alert 2", "Alert 1"]
    assert page["meta"]["has_more"] is True
    cursor = page["meta"]["next_cursor"]
    rest = (
        await client.get(url, headers=owner.headers, params={"limit": 2, "cursor": cursor})
    ).json()
    assert [n["title"] for n in rest["data"]] == ["Alert 0"] and rest["meta"]["has_more"] is False
    bad = await client.get(url, headers=owner.headers, params={"cursor": "not-a-cursor"})
    assert err(bad, 422)["details"][0]["field"] == "cursor"
    assert (await client.get(url, headers=owner.headers, params={"limit": 0})).status_code == 422

    read = await client.post(f"{url}/{data[1]['id']}/read", headers=owner.headers)
    assert ok_data(read)["is_read"] is True
    only_unread = await inbox(client, owner, org, unread="true")
    assert [n["title"] for n in only_unread] == ["Alert 2", "Alert 0"]
    assert await unread(client, owner, org) == 2


async def test_marking_read_is_idempotent_and_read_all_counts_what_it_changed(client):
    owner, org = await practice(client)
    for n in range(3):
        await notify(org, "budget_alert", [owner], title=f"Alert {n}")
    url = org_url(org, "/notifications")
    target = (await inbox(client, owner, org))[0]["id"]

    first = ok_data(await client.post(f"{url}/{target}/read", headers=owner.headers))
    again = ok_data(await client.post(f"{url}/{target}/read", headers=owner.headers))
    assert first["read_at"] is not None and again["read_at"] == first["read_at"]
    assert await unread(client, owner, org) == 2

    marked = await client.post(f"{url}/read-all", headers=owner.headers)
    assert ok_data(marked) == {"updated": 2}
    assert await unread(client, owner, org) == 0
    assert ok_data(await client.post(f"{url}/read-all", headers=owner.headers)) == {"updated": 0}
    assert await inbox(client, owner, org, unread="true") == []
    assert len(await inbox(client, owner, org)) == 3
    missing = await client.post(f"{url}/{uuid.uuid4()}/read", headers=owner.headers)
    assert err(missing, 404)["code"] == "RESOURCE_NOT_FOUND"
    assert (await client.post(f"{url}/not-a-uuid/read", headers=owner.headers)).status_code == 422


async def test_notifications_are_private_and_belong_to_one_practice(client):
    owner, org = await practice(client)
    alice = await staff(org, "staff")
    outsider, other = await practice(client, "Other Practice")
    await notify(org, "budget_alert", [alice], title="For Alice")
    [mine] = await inbox(client, alice, org)

    # Another person of the practice neither sees it nor can mark it.
    assert await inbox(client, owner, org) == []
    url = org_url(org, "/notifications")
    assert err(await client.post(f"{url}/{mine['id']}/read", headers=owner.headers), 404)
    assert (await inbox(client, alice, org))[0]["is_read"] is False
    # Someone from another practice is not even a member: 404 for the whole practice.
    assert (await client.get(url, headers=outsider.headers)).status_code == 404
    assert (await client.get(url + "/unread-count", headers=outsider.headers)).status_code == 404
    assert (await client.post(f"{url}/read-all", headers=outsider.headers)).status_code == 404
    # The id means nothing in another practice, even to the person it belongs to.
    await add_member(other["id"], "staff", user=alice)
    elsewhere = org_url(other, "/notifications")
    assert (
        await client.post(f"{elsewhere}/{mine['id']}/read", headers=alice.headers)
    ).status_code == 404
    assert await inbox(client, alice, other) == []
    assert await unread(client, alice, other) == 0 and await unread(client, alice, org) == 1


async def test_a_staff_member_who_has_notifications_can_still_be_deleted(client):
    owner, org = await practice(client)
    person = await staff(org, "staff")
    await notify(org, "budget_alert", [person])
    prefs = {
        "preferences": [{"type": "budget_alert", "in_app": True, "email": False, "sms": False}]
    }
    await put_own(client, person, org, prefs)
    assert await count_rows("SELECT count(*) FROM notifications") == 1
    assert await count_rows("SELECT count(*) FROM notification_preferences") == 1

    # Their inbox and settings are not history: they do not make the person "in use".
    deleted = await client.delete(org_url(org, f"/staff/{person.id}"), headers=owner.headers)
    assert deleted.status_code == 200, deleted.text
    assert await count_rows("SELECT count(*) FROM notifications") == 0
    assert await count_rows("SELECT count(*) FROM notification_preferences") == 0


# --- notify -------------------------------------------------------------------------------------


async def test_notify_is_idempotent_per_dedupe_key(client):
    owner, org = await practice(client)
    alice = await staff(org, "staff")

    first = await notify(org, "budget_alert", [owner, alice], dedupe_key="budget:1")
    assert (first.created, first.duplicates, first.emails_queued) == (2, 0, 2)
    again = await notify(org, "budget_alert", [owner, alice], dedupe_key="budget:1")
    assert (again.created, again.duplicates, again.emails_queued) == (0, 2, 0)
    assert await count_rows("SELECT count(*) FROM notifications") == 2
    assert len(await notification_jobs("email")) == 2

    # Another person gets their own; another key, and no key at all, notify again.
    carol = await staff(org, "staff")
    third = await notify(org, "budget_alert", [owner, alice, carol], dedupe_key="budget:1")
    assert (third.created, third.duplicates) == (1, 2)
    await notify(org, "budget_alert", [owner], dedupe_key="budget:2")
    await notify(org, "budget_alert", [owner])
    await notify(org, "budget_alert", [owner])
    assert (
        await count_rows("SELECT count(*) FROM notifications WHERE user_id = :u", u=owner.id) == 4
    )
    # A key belongs to the person whatever the type, which is why the helpers start theirs with
    # the type: the same text for another type is a duplicate, a prefixed key is not.
    clash = await notify(org, "payment_reminder", [owner], dedupe_key="budget:1")
    assert (clash.created, clash.duplicates) == (0, 1)
    assert dedupe_key("payment_reminder", "budget:1") == "payment_reminder:budget:1"
    await notify(org, "payment_reminder", [owner], dedupe_key=dedupe_key("payment_reminder", 1))
    assert (
        await count_rows("SELECT count(*) FROM notifications WHERE user_id = :u", u=owner.id) == 5
    )


async def test_email_only_recipients_are_not_emailed_twice_either(client, worker, mailbox):
    _, org = await practice(client)
    carol = await staff(org, "staff")
    prefs = {
        "preferences": [{"type": "budget_alert", "in_app": False, "email": True, "sms": False}]
    }
    await put_own(client, carol, org, prefs)

    first = await notify(org, "budget_alert", [carol], dedupe_key="budget:1")
    assert (first.created, first.emails_queued) == (0, 1)
    assert await inbox(client, carol, org) == []
    duplicate = await notify(org, "budget_alert", [carol], dedupe_key="budget:1")
    assert duplicate.emails_queued == 0
    assert len(await notification_jobs("email")) == 1  # the live job guards the key
    await run_jobs(worker)
    assert len(mailbox.to(carol.email)) == 1
    # Even after delivery, the finished job still remembers the key.
    later = await notify(org, "budget_alert", [carol], dedupe_key="budget:1")
    assert later.emails_queued == 0
    assert await run_jobs(worker) == 0 and len(mailbox.to(carol.email)) == 1


async def test_people_who_cannot_be_notified_are_skipped(client):
    owner, org = await practice(client)
    active = await staff(org, "staff")
    suspended = await add_member(org["id"], "staff", status="suspended")
    removed = await add_member(org["id"], "staff", status="removed")
    no_login = await new_staff(client, owner, org, "No Login")
    _, other = await practice(client, "Other Practice")
    outsider = await staff(other, "staff")

    result = await notify(
        org, "budget_alert", [active, suspended, removed, no_login["id"], outsider, uuid.uuid4()]
    )
    assert (result.created, result.unavailable, result.emails_queued) == (1, 5, 1)
    assert [n["type"] for n in await inbox(client, active, org)] == ["budget_alert"]
    rows = await sql("SELECT user_id FROM notifications")
    assert [r["user_id"] for r in rows] == [active.id]


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/x",
        "//evil.example",
        "/\\evil.example",
        "javascript:alert(1)",
        "time/approvals",
        "/a b",
        "/tab\there",
        "/" + "x" * 300,
    ],
)
async def test_a_link_must_be_a_path_inside_the_app(client, link):
    owner, org = await practice(client)
    with pytest.raises(ValueError, match="link must be a path"):
        await notify(org, "budget_alert", [owner], link=link)
    assert await count_rows("SELECT count(*) FROM notifications") == 0


async def test_valid_links_are_stored_and_the_database_refuses_the_rest(client):
    owner, org = await practice(client)
    for link in (None, "/", "/time/approvals", "/jobs/1?tab=time#top"):
        await notify(org, "budget_alert", [owner], link=link)
    assert len(await inbox(client, owner, org)) == 4
    for bad in ("https://evil.example", "//evil.example", "/x\\y", "relative/path"):
        with pytest.raises(IntegrityError):
            await sql(
                "INSERT INTO notifications (id, organization_id, user_id, type, title, link) "
                "VALUES (:id, :org, :user, 'budget_alert', 't', :link)",
                id=uuid.uuid4(),
                org=org["id"],
                user=owner.id,
                link=bad,
            )


async def test_notify_refuses_programming_errors(client):
    owner, org = await practice(client)
    with pytest.raises(ValueError, match="unknown notification type"):
        await notify(org, "no_such_type", [owner])
    with pytest.raises(ValueError, match="needs a title"):
        await notify(org, "budget_alert", [owner], title="   ")
    with pytest.raises(ValueError, match="go together"):
        await notify(org, "budget_alert", [owner], entity_type="job")
    assert await count_rows("SELECT count(*) FROM notifications") == 0


async def test_long_text_is_clipped_and_the_payload_is_json(client):
    owner, org = await practice(client)
    entity = uuid.uuid4()
    await notify(
        org,
        "budget_alert",
        [owner],
        title="T" * 500,
        body="B" * 5000,
        entity_type="job",
        entity_id=entity,
        payload={"id": entity, "on": date(2026, 10, 1), "nested": {"n": 1}},
    )
    [row] = await inbox(client, owner, org)
    assert len(row["title"]) == 200 and row["title"].endswith("…") and len(row["body"]) == 2000
    assert row["entity_type"] == "job" and row["entity_id"] == str(entity)
    assert row["payload"] == {"id": str(entity), "on": "2026-10-01", "nested": {"n": 1}}


async def test_the_catalogue_lists_its_types_and_a_template_for_each(client):
    assert [t.code for t in CATALOGUE] == [
        "timesheet_reminder",
        "timesheet_returned",
        "approval_pending",
        "budget_alert",
        "invoice_delivery",
        "payment_reminder",
        "failed_job",
        "expense_pending",
        "expense_approved",
        "expense_returned",
        "invoice_approval_pending",
        "invoice_approved",
        "invoice_rejected",
        "payment_reversed",
    ]
    assert {t.code for t in CATALOGUE} == {str(t) for t in NotificationType}


# --- Delivery by email --------------------------------------------------------------------------


async def test_the_email_channel_delivers_the_notification_through_the_worker(
    client, worker, mailbox
):
    owner, org = await practice(client, "Acme Accountants")
    await notify(
        org,
        "budget_alert",
        [owner],
        title="Budget alert: Year-end accounts",
        link="/jobs/123",
        merge={"job_name": "Year-end accounts", "client_name": "Widgets Ltd", "budget_percent": 80},
    )
    [job] = await notification_jobs("email")
    assert "@" not in str(job), "the job carries ids, never an address"
    assert await run_jobs(worker) == 1

    [message] = mailbox.to(owner.email)
    assert message["Subject"] == "Budget alert: Year-end accounts has used 80% of its budget"
    text = Mailbox.text(message)
    assert "Widgets Ltd" in text and "Acme Accountants" in text
    assert f"http://localhost:5173/{org['slug']}/jobs/123" in text
    assert "Hi User" in text  # the person's own name
    statuses = await sql("SELECT status FROM background_jobs WHERE type = 'send_notification'")
    assert [s["status"] for s in statuses] == ["succeeded"]


async def test_a_recipient_who_left_before_delivery_is_not_messaged(client, worker, mailbox):
    _, org = await practice(client)
    leaver = await staff(org, "staff")
    await notify(org, "budget_alert", [leaver])
    await sql(
        "UPDATE organization_members SET status = 'suspended' "
        "WHERE organization_id = :org AND user_id = :user",
        org=org["id"],
        user=leaver.id,
    )
    assert await run_jobs(worker) == 1
    assert mailbox.to(leaver.email) == []
    [row] = await sql(
        "SELECT status, payload FROM background_jobs WHERE type = 'send_notification'"
    )
    assert row["status"] == "succeeded" and row["payload"]["skipped"] == "recipient_unavailable"


# --- Triggers: timesheets -----------------------------------------------------------------------


async def test_submitting_a_timesheet_notifies_the_persons_manager(client):
    owner, org = await practice(client)
    person = await staff(org, "staff", full_name="Pat Person")
    manager = await staff(org, "manager", full_name="Mo Manager")
    other_manager = await staff(org, "manager", full_name="Olly Other")
    await set_manager(org, person, manager)
    sheet_id = await sheet_of(client, person, org, minutes=90)
    await act(client, person, org, sheet_id, "submit")

    [note] = await inbox(client, manager, org)
    assert note["type"] == "approval_pending" and note["link"] == "/time/approvals"
    assert (note["entity_type"], note["entity_id"]) == ("timesheet", sheet_id)
    assert note["title"] == "Timesheet waiting for approval"
    assert "Pat Person" in note["body"] and "07/09/2026" in note["body"] and "1:30" in note["body"]
    # Only the manager: not the other approvers, not the person.
    for other in (owner, other_manager, person):
        assert await inbox(client, other, org) == []


async def test_without_a_manager_everyone_who_can_approve_is_notified(client):
    owner, org = await practice(client)
    person = await staff(org, "staff", full_name="Pat Person")
    manager = await staff(org, "manager")
    admin = await staff(org, "admin")
    viewer = await staff(org, "viewer")
    colleague = await staff(org, "staff")
    sheet_id = await sheet_of(client, person, org)
    await act(client, person, org, sheet_id, "submit")
    for approver in (owner, manager, admin):
        assert [n["type"] for n in await inbox(client, approver, org)] == ["approval_pending"]
    for other in (person, viewer, colleague):
        assert await inbox(client, other, org) == []


async def test_a_manager_who_cannot_approve_does_not_replace_the_approvers(client):
    owner, org = await practice(client)
    person = await staff(org, "staff")
    named = await staff(org, "staff", full_name="Not An Approver")
    approver = await staff(org, "manager")
    await set_manager(org, person, named)
    await act(client, person, org, await sheet_of(client, person, org), "submit")
    assert await inbox(client, named, org) == []
    assert (
        len(await inbox(client, approver, org)) == 1 and len(await inbox(client, owner, org)) == 1
    )


async def test_a_manager_who_submits_is_not_notified_about_their_own_sheet(client):
    owner, org = await practice(client)
    manager = await staff(org, "manager")
    await act(client, manager, org, await sheet_of(client, manager, org), "submit")
    assert await inbox(client, manager, org) == []
    assert len(await inbox(client, owner, org)) == 1


async def test_rejecting_a_timesheet_notifies_its_owner_each_time(client):
    _, org = await practice(client)
    person = await staff(org, "staff", full_name="Pat Person")
    manager = await staff(org, "manager", full_name="Mo Manager")
    await set_manager(org, person, manager)
    sheet_id = await sheet_of(client, person, org)
    await act(client, person, org, sheet_id, "submit")
    await act(client, manager, org, sheet_id, "reject", {"decision_note": "Add detail"})

    [returned] = await inbox(client, person, org, unread="true")
    assert returned["type"] == "timesheet_returned" and returned["link"] == "/time"
    assert returned["entity_id"] == sheet_id and returned["title"] == "Timesheet returned"
    assert "Mo Manager" in returned["body"] and "Add detail" in returned["body"]
    assert await inbox(client, manager, org, unread="true") != []  # the submission, not the return
    assert all(n["type"] != "timesheet_returned" for n in await inbox(client, manager, org))

    # A second rejection is a new event.
    await act(client, person, org, sheet_id, "submit")
    await act(client, manager, org, sheet_id, "reject", {"decision_note": "Still missing"})
    notes = [n for n in await inbox(client, person, org) if n["type"] == "timesheet_returned"]
    assert len(notes) == 2 and "Still missing" in notes[0]["body"]
    # ... and each submission notified the manager.
    assert (
        len([n for n in await inbox(client, manager, org) if n["type"] == "approval_pending"]) == 2
    )


async def test_a_switched_off_type_sends_no_timesheet_notifications(client):
    owner, org = await practice(client)
    person = await staff(org, "staff")
    manager = await staff(org, "manager")
    off = {
        "type": "approval_pending",
        "enabled": False,
        "in_app": True,
        "email": True,
        "sms": False,
    }
    await put_practice(client, owner, org, {"types": [off]})
    await act(client, person, org, await sheet_of(client, person, org), "submit")
    assert await inbox(client, manager, org) == [] and await inbox(client, owner, org) == []
    assert await notification_jobs() == []


async def test_a_notification_problem_never_fails_the_business_action(client, monkeypatch):
    _, org = await practice(client)
    person = await staff(org, "staff")
    manager = await staff(org, "manager")

    async def broken(self, **kwargs):
        raise RuntimeError("notification store is down")

    monkeypatch.setattr(NotificationService, "notify", broken)
    sheet_id = await sheet_of(client, person, org)
    submitted = await act(client, person, org, sheet_id, "submit")
    assert submitted["status"] == "submitted"
    rejected = await act(client, manager, org, sheet_id, "reject", {"decision_note": "No"})
    assert rejected["status"] == "rejected"
    assert await count_rows("SELECT count(*) FROM notifications") == 0


# --- Triggers: invoice delivery -----------------------------------------------------------------


async def test_the_sender_is_notified_when_the_invoice_email_is_delivered(client, worker, mailbox):
    p = await new_practice(client)
    emailed = await make_client(p.org, "Emailed Ltd", email="bills@emailed.example")
    entry = await make_time_entry(p.org, p.owner, client=emailed)
    invoice = await draft(client, p.owner, p.org, emailed.id, time=[entry])
    await issued(client, p.owner, p.org, invoice["id"])
    url = org_url(p.org, f"/invoices/{invoice['id']}/send")

    assert (await client.post(url, headers=p.owner.headers)).status_code == 202
    assert await inbox(client, p.owner, p.org) == []  # nothing before it is delivered
    assert await run_jobs(worker) == 1
    [note] = await inbox(client, p.owner, p.org)
    assert note["type"] == "invoice_delivery" and note["title"] == "Invoice INV-00001 sent"
    assert note["link"] == f"/billing/invoices/{invoice['id']}"
    assert (note["entity_type"], note["entity_id"]) == ("invoice", invoice["id"])
    assert "bills@emailed.example" in note["body"] and "£144.00" in note["body"]
    # In-app only by default: the sender is not emailed about their own email.
    assert len(mailbox.to("bills@emailed.example")) == 1 and mailbox.to(p.owner.email) == []

    # Sending again is a new request and a new notification.
    assert (await client.post(url, headers=p.owner.headers)).status_code == 202
    await run_jobs(worker)
    assert len(await inbox(client, p.owner, p.org)) == 2


# --- Triggers: failed jobs ----------------------------------------------------------------------


async def enqueue_failing(org: dict | None, job_type: str = "flaky", **kwargs) -> None:
    async with get_sessionmaker()() as session:
        await jobs.enqueue(
            session,
            job_type,
            kwargs.pop("payload", {}),
            organization_id=uuid.UUID(org["id"]) if org else None,
            **kwargs,
        )
        await session.commit()


@pytest.mark.parametrize("failure", ["retries_used_up", "permanent"])
async def test_a_job_that_fails_for_good_notifies_the_admins_once(
    client, worker, monkeypatch, failure
):
    owner, org = await practice(client)
    admin = await staff(org, "admin")
    partner = await staff(org, "partner")
    manager = await staff(org, "manager")
    person = await staff(org, "staff")

    async def boom(session, settings, payload):
        if failure == "permanent":
            raise handlers.PermanentJobError("can never work")
        raise ConnectionError("provider down")

    monkeypatch.setitem(handlers.HANDLERS, "flaky", boom)
    await enqueue_failing(org, max_attempts=1)
    await worker.run_once()

    for admin_like in (owner, admin, partner):
        [note] = await inbox(client, admin_like, org)
        assert note["type"] == "failed_job" and note["title"] == "A background job failed"
        assert note["entity_type"] == "background_job" and note["link"] is None
        assert "Flaky failed after 1 attempt" in note["body"]
        assert note["payload"]["status"] == ("failed" if failure == "permanent" else "dead")
    for other in (manager, person):
        assert await inbox(client, other, org) == []
    # Once per job: the dedupe key is the job.
    assert (
        await count_rows("SELECT count(*) FROM notifications WHERE dedupe_key LIKE 'failed_job:%'")
        == 3
    )


async def test_a_job_that_will_be_retried_does_not_notify_yet(client, worker, monkeypatch):
    owner, org = await practice(client)

    async def boom(session, settings, payload):
        raise ConnectionError("provider down")

    monkeypatch.setitem(handlers.HANDLERS, "flaky", boom)
    await enqueue_failing(org, max_attempts=2)
    await worker.run_once()
    assert await inbox(client, owner, org) == []
    await sql("UPDATE background_jobs SET run_at = now() WHERE status = 'queued'")
    await worker.run_once()
    assert [n["type"] for n in await inbox(client, owner, org)] == ["failed_job"]


async def test_jobs_of_no_practice_and_failed_deliveries_do_not_notify(client, worker):
    owner, org = await practice(client)
    await enqueue_failing(None, "no_such_handler")
    # A notification whose own delivery fails would only notify about itself.
    await enqueue_failing(org, "send_notification", payload={"channel": "email"})
    await worker.run_once()
    statuses = await sql("SELECT type, status FROM background_jobs ORDER BY type")
    assert [(s["type"], s["status"]) for s in statuses] == [
        ("no_such_handler", "failed"),
        ("send_notification", "failed"),
    ]
    assert await inbox(client, owner, org) == []


async def test_a_job_whose_lease_expired_for_the_last_time_notifies(client, worker):
    owner, org = await practice(client)
    await enqueue_failing(org, "purge_retention", max_attempts=1)
    await sql(
        "UPDATE background_jobs SET status = 'running', attempts = 1, locked_by = 'crashed', "
        "locked_until = now() - interval '1 minute' WHERE type = 'purge_retention'"
    )
    await worker.tick()
    [note] = await inbox(client, owner, org)
    assert note["type"] == "failed_job" and "Purge retention failed after 1 attempt" in note["body"]
    # The reaper does it once.
    await worker.tick()
    assert len(await inbox(client, owner, org)) == 1


# --- The helpers later modules call -------------------------------------------------------------


async def test_the_ready_made_helpers_build_complete_notifications(client):
    owner, org = await practice(client)
    org_id = uuid.UUID(org["id"])
    job_id, invoice_id = uuid.uuid4(), uuid.uuid4()
    async with get_sessionmaker()() as session:
        for _ in range(2):  # the second round changes nothing: same dedupe keys
            await notification_events.budget_alert(
                session,
                organization_id=org_id,
                recipients=[owner.id],
                job_id=job_id,
                job_name="Year-end accounts",
                client_name="Widgets Ltd",
                percent=80,
            )
            await notification_events.timesheet_reminder(
                session,
                organization_id=org_id,
                recipients=[owner.id],
                week_start=date(2026, 10, 12),
            )
            await notification_events.payment_reminder(
                session,
                organization_id=org_id,
                recipients=[owner.id],
                invoice_id=invoice_id,
                invoice_number="INV-00042",
                client_name="Widgets Ltd",
                amount_due="£1,200.00",
                due_date=date(2026, 9, 30),
                days_overdue=14,
                stage="first",
            )
        await session.commit()
    by_type = {n["type"]: n for n in await inbox(client, owner, org)}
    assert set(by_type) == {"budget_alert", "timesheet_reminder", "payment_reminder"}
    budget = by_type["budget_alert"]
    assert (
        budget["link"] == f"/jobs/{job_id}" and budget["title"] == "Budget alert: Year-end accounts"
    )
    assert (budget["entity_type"], budget["entity_id"]) == ("job", str(job_id))
    assert "80%" in budget["body"] and budget["payload"] == {"percent": 80}
    reminder = by_type["timesheet_reminder"]
    assert reminder["link"] == "/time" and "12/10/2026" in reminder["body"]
    payment = by_type["payment_reminder"]
    assert payment["link"] == f"/billing/invoices/{invoice_id}"
    assert "14 days overdue" in payment["body"] and "30/09/2026" in payment["body"]
    # Each queued one email (on by default), once.
    assert sorted(j["type"] for j in await notification_jobs("email")) == [
        "budget_alert",
        "payment_reminder",
        "timesheet_reminder",
    ]
    # A new threshold or stage is a new notification.
    async with get_sessionmaker()() as session:
        await notification_events.budget_alert(
            session,
            organization_id=org_id,
            recipients=[owner.id],
            job_id=job_id,
            job_name="Year-end accounts",
            client_name="Widgets Ltd",
            percent=100,
        )
        await session.commit()
    assert len(await inbox(client, owner, org)) == 4


# --- Housekeeping -------------------------------------------------------------------------------


async def test_the_retention_job_clears_old_notifications_and_the_mock_log(client):
    owner, org = await practice(client)
    for title in ("Old", "Recent"):
        await notify(org, "budget_alert", [owner], title=title)
    await sql(
        "UPDATE notifications SET created_at = now() - interval '200 days' WHERE title = 'Old'"
    )
    gateway = build_gateway(mock_settings())
    for subject in ("Old", "Recent"):
        await gateway.send_email(
            OutboundEmail(
                to="a@example.com", subject=subject, html="h", text="t", template="welcome"
            )
        )
    await sql(
        "UPDATE message_log SET created_at = now() - interval '100 days' WHERE subject = 'Old'"
    )

    async with get_sessionmaker()() as session:
        await handlers.HANDLERS["purge_retention"](session, get_settings(), {})
        await session.commit()
    assert [n["title"] for n in await inbox(client, owner, org)] == ["Recent"]
    assert [row["subject"] for row in await message_log()] == ["Recent"]


# --- Documentation ------------------------------------------------------------------------------

PREFIX = "/api/v1/organizations/{organization_id}"
EXPECTED = {
    ("get", "/notifications"): "Notifications",
    ("get", "/notifications/unread-count"): "Notifications",
    ("post", "/notifications/read-all"): "Notifications",
    ("post", "/notifications/{notification_id}/read"): "Notifications",
    ("get", "/notification-settings"): "Notifications",
    ("put", "/notification-settings"): "Notifications",
    ("get", "/notification-settings/practice"): "Notifications",
    ("put", "/notification-settings/practice"): "Notifications",
    ("get", "/email-templates"): "Email templates",
    ("get", "/email-templates/{key}"): "Email templates",
    ("put", "/email-templates/{key}"): "Email templates",
    ("delete", "/email-templates/{key}"): "Email templates",
    ("post", "/email-templates/{key}/preview"): "Email templates",
}
ADMIN_ONLY = {path for (_, path) in EXPECTED if "practice" in path or "email-templates" in path}


async def test_the_routes_are_documented(client):
    schema = (await client.get("/openapi.json")).json()
    for (method, path), tag in EXPECTED.items():
        operation = schema["paths"][PREFIX + path][method]
        assert operation["summary"], (method, path)
        assert len(operation["description"]) > 20, (method, path)
        assert tag in operation["tags"], (method, path)
        assert {"401", "404", "422"} <= set(operation["responses"]), (method, path)
        assert ("403" in operation["responses"]) is (path in ADMIN_ONLY), (method, path)
    listed = schema["paths"][PREFIX + "/notifications"]["get"]["parameters"]
    assert all(p.get("description") for p in listed if p["in"] == "query")

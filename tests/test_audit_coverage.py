"""Spec 2.20: every important action leaves one audit entry with who, what, where from and, where
it applies, what changed. Each test drives one operation through the API and reads the trail."""

import uuid
from datetime import timedelta

from tests.audit_helpers import audit_url, entry_rows, history, listing
from tests.billing_helpers import draft, hourly_job, issued, new_practice, pay
from tests.clients_helpers import create_client, org_url, sql
from tests.domain_factories import make_client, make_job, make_time_entry, practice, staff
from tests.expense_helpers import act, create_claim
from tests.factories import API, add_member, role_id
from tests.foundation_helpers import create_person, sign_in
from tests.period_lock_helpers import lock_through, reopen_to, today
from tests.team_helpers import new_rate, new_staff
from tests.time_helpers import log_time

DAY = timedelta(days=1)


async def only(org: dict, action: str, *, actor, target_type: str, target_id=None) -> dict:
    """The one entry of ``action``: by the caller, about the record, from an address, with a
    request id and a user agent."""
    rows = await entry_rows(org, action)
    assert len(rows) == 1, (action, rows)
    [row] = rows
    assert row["actor_user_id"] == str(actor.id), action
    assert row["target_type"] == target_type, action
    if target_id is not None:
        assert row["target_id"] == str(target_id), action
    else:
        assert row["target_id"], action
    assert row["ip"] == "127.0.0.1", action
    assert row["request_id"] and row["user_agent"], action
    return row


async def changes(client, actor, org, target_type, target_id, action) -> list[dict]:
    """What the record's history says changed in the entry of ``action``."""
    response = await history(client, actor, org, target_type, target_id)
    assert response.status_code == 200, response.text
    [entry] = [e for e in response.json()["data"] if e["action"] == action]
    return entry["changes"]


# --- Create, update, delete -------------------------------------------------------------------


async def test_creating_a_client_is_audited(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, "Acme Ltd")
    row = await only(org, "client.created", actor=owner, target_type="client", target_id=acme["id"])
    assert row["metadata"] == {"code": acme["code"], "name": "Acme Ltd", "status": acme["status"]}


async def test_updating_a_client_records_before_and_after(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, "Acme Ltd")
    response = await client.patch(
        org_url(org, f"/clients/{acme['id']}"),
        headers=owner.headers,
        json={"name": "Acme Group", "segment": "Retail"},
    )
    assert response.status_code == 200, response.text
    row = await only(org, "client.updated", actor=owner, target_type="client", target_id=acme["id"])
    assert row["metadata"]["changes"] == {
        "name": ["Acme Ltd", "Acme Group"],
        "segment": [None, "Retail"],
    }
    shown = await changes(client, owner, org, "client", acme["id"], "client.updated")
    assert {c["field"]: (c["before"], c["after"]) for c in shown} == {
        "name": ("Acme Ltd", "Acme Group"),
        "segment": (None, "Retail"),
    }
    # An update that changes nothing writes nothing.
    again = await client.patch(
        org_url(org, f"/clients/{acme['id']}"), headers=owner.headers, json={"name": "Acme Group"}
    )
    assert again.status_code == 200
    assert len(await entry_rows(org, "client.updated")) == 1


async def test_archiving_and_deleting_a_client_are_audited(client):
    owner, org = await practice(client)
    keep = await create_client(client, owner, org, "Keep Ltd")
    gone = await create_client(client, owner, org, "Gone Ltd")
    archived = await client.post(
        org_url(org, f"/clients/{keep['id']}/archive"), headers=owner.headers
    )
    assert archived.status_code == 200, archived.text
    row = await only(
        org, "client.archived", actor=owner, target_type="client", target_id=keep["id"]
    )
    assert row["metadata"]["changes"]["status"][1] == "archived"

    deleted = await client.delete(org_url(org, f"/clients/{gone['id']}"), headers=owner.headers)
    assert deleted.status_code in (200, 204), deleted.text
    row = await only(org, "client.deleted", actor=owner, target_type="client", target_id=gone["id"])
    assert row["metadata"] == {"code": gone["code"], "name": "Gone Ltd"}
    # A deleted record keeps its history.
    shown = await history(client, owner, org, "client", gone["id"])
    assert [e["action"] for e in shown.json()["data"]] == ["client.deleted", "client.created"]


async def test_changing_the_practice_settings_records_before_and_after(client):
    owner, org = await practice(client)
    response = await client.patch(
        f"{API}/organizations/{org['id']}", headers=owner.headers, json={"payment_terms_days": 14}
    )
    assert response.status_code == 200, response.text
    row = await only(
        org, "organization.updated", actor=owner, target_type="organization", target_id=org["id"]
    )
    old = row["metadata"]["changes"]["payment_terms_days"][0]
    assert row["metadata"]["changes"] == {"payment_terms_days": [old, 14]}
    shown = await changes(client, owner, org, "organization", org["id"], "organization.updated")
    assert shown == [{"field": "payment_terms_days", "before": old, "after": 14}]


# --- Sign-in ----------------------------------------------------------------------------------


async def test_signing_in_is_audited_with_the_person_the_session_and_the_address(client):
    owner, org = await practice(client)
    created = await create_person(client, owner, org, email="newbie@example.com")
    person = await sign_in(client, "newbie@example.com", created["temporary_password"])

    rows = await sql(
        "SELECT organization_id, actor_user_id, target_type, target_id, host(ip_address) AS ip, "
        "user_agent, request_id FROM audit_logs WHERE action = 'auth.login' AND actor_user_id = :u",
        u=str(person.id),
    )
    assert len(rows) == 1
    [row] = rows
    session_id = await sql("SELECT id FROM user_sessions WHERE user_id = :u", u=str(person.id))
    assert row["target_type"] == "session" and row["target_id"] == session_id[0]["id"]
    assert row["ip"] == "127.0.0.1" and row["request_id"] and row["user_agent"]
    # Sign-ins belong to the account, not to a practice: they are not in the practice's log.
    assert row["organization_id"] is None
    listed = await listing(client, owner, org, action="auth.")
    assert listed.json()["data"] == []


async def test_a_failed_sign_in_is_audited(client):
    owner, org = await practice(client)
    created = await create_person(client, owner, org, email="newbie@example.com")
    refused = await client.post(
        f"{API}/auth/login", json={"email": "newbie@example.com", "password": "not the password"}
    )
    assert refused.status_code == 401
    rows = await sql(
        "SELECT actor_user_id, host(ip_address) AS ip, request_id FROM audit_logs "
        "WHERE action = 'auth.login_failed'"
    )
    assert len(rows) == 1
    assert str(rows[0]["actor_user_id"]) == created["member"]["user_id"]
    assert rows[0]["ip"] == "127.0.0.1" and rows[0]["request_id"]


# --- Approval ---------------------------------------------------------------------------------


async def test_approving_a_timesheet_is_audited(client):
    owner, org = await practice(client)
    person = await staff(org, full_name="Pat Person")
    manager = await staff(org, "manager")
    logged = await log_time(client, person, org)
    sheet_id = logged["timesheet_id"]
    base = org_url(org, f"/timesheets/{sheet_id}")
    assert (await client.post(f"{base}/submit", headers=person.headers)).status_code == 200
    assert (await client.post(f"{base}/approve", headers=manager.headers)).status_code == 200

    row = await only(
        org, "timesheet.approved", actor=manager, target_type="timesheet", target_id=sheet_id
    )
    assert (row["metadata"]["old_status"], row["metadata"]["new_status"]) == (
        "submitted",
        "approved",
    )
    assert row["metadata"]["user_name"] == "Pat Person"
    assert await changes(client, owner, org, "timesheet", sheet_id, "timesheet.approved") == [
        {"field": "status", "before": "submitted", "after": "approved"}
    ]
    await only(
        org, "timesheet.submitted", actor=person, target_type="timesheet", target_id=sheet_id
    )


# --- Billing ----------------------------------------------------------------------------------


async def test_issuing_an_invoice_taking_a_payment_and_writing_off_are_audited(client):
    p = await new_practice(client)
    job = await hourly_job(p)
    billed = await make_time_entry(p.org, p.owner, client=p.client, job=job)
    written_off = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=30)

    created = await draft(client, p.owner, p.org, p.client.id, time=[billed])
    invoice = await issued(client, p.owner, p.org, created["id"])
    row = await only(
        p.org, "invoice.issued", actor=p.owner, target_type="invoice", target_id=invoice["id"]
    )
    assert row["metadata"]["number"] == invoice["number"]
    assert row["metadata"]["gross_total"] == invoice["gross_total"]
    assert row["metadata"]["client_name"] == "Widgets Ltd"

    received = await pay(
        client, p.owner, p.org, p.client.id, invoice["gross_total"], auto_allocate=True
    )
    assert received.status_code == 201, received.text
    payment_id = received.json()["data"]["id"]
    row = await only(
        p.org, "payment.recorded", actor=p.owner, target_type="payment", target_id=payment_id
    )
    assert row["metadata"]["client_name"] == "Widgets Ltd"
    await only(
        p.org, "payment.allocated", actor=p.owner, target_type="payment", target_id=payment_id
    )

    response = await client.post(
        p.url("/wip/write-offs"),
        headers=p.owner.headers,
        json={
            "client_id": str(p.client.id),
            "items": [{"time_entry_id": str(written_off.id)}],
            "reason": "Agreed with the client",
        },
    )
    assert response.status_code == 201, response.text
    row = await only(
        p.org,
        "wip.written_off",
        actor=p.owner,
        target_type="wip_write_off",
        target_id=response.json()["data"]["id"],
    )
    assert row["metadata"]["reason"] == "Agreed with the client"
    # The client's history gathers all of it.
    shown = await history(client, p.owner, p.org, "client", p.client.id, include_related="true")
    assert {"invoice.issued", "payment.recorded", "wip.written_off"} <= {
        e["action"] for e in shown.json()["data"]
    }


# --- Rates, period locks, status changes ------------------------------------------------------


async def test_changing_a_rate_records_before_and_after(client):
    owner, org = await practice(client)
    person = await staff(org)
    rate = await new_rate(
        client, owner, org, kind="charge", user_id=person.id, rate=9000, effective_from="2026-01-01"
    )
    await only(org, "rate.created", actor=owner, target_type="rate_rule", target_id=rate["id"])
    response = await client.patch(
        org_url(org, f"/rates/{rate['id']}"), headers=owner.headers, json={"rate": 9500}
    )
    assert response.status_code == 200, response.text
    row = await only(
        org, "rate.updated", actor=owner, target_type="rate_rule", target_id=rate["id"]
    )
    assert row["metadata"]["changes"] == {"rate": [9000, 9500]}
    assert await changes(client, owner, org, "rate_rule", rate["id"], "rate.updated") == [
        {"field": "rate", "before": 9000, "after": 9500}
    ]


async def test_locking_and_reopening_a_period_are_audited(client):
    owner, org = await practice(client)
    t = today(org)
    assert (
        await lock_through(client, owner, org, t - 30 * DAY, reason="March close")
    ).status_code == 200
    lock_id = (await sql("SELECT id FROM period_locks WHERE organization_id = :o", o=org["id"]))[0][
        "id"
    ]
    row = await only(
        org, "period_lock.locked", actor=owner, target_type="period_lock", target_id=lock_id
    )
    assert row["metadata"]["reason"] == "March close"
    assert (
        await reopen_to(client, owner, org, t - 40 * DAY, reason="A late invoice")
    ).status_code == 200
    row = await only(
        org, "period_lock.reopened", actor=owner, target_type="period_lock", target_id=lock_id
    )
    assert row["metadata"]["reason"] == "A late invoice"
    assert await changes(client, owner, org, "period_lock", lock_id, "period_lock.reopened") == [
        {
            "field": "locked_through",
            "before": str(t - 30 * DAY),
            "after": str(t - 40 * DAY),
        }
    ]


async def test_changing_a_jobs_status_records_before_and_after(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    job = await make_job(org, acme, "Accounts", status="planned")
    response = await client.post(
        org_url(org, f"/jobs/{job.id}/status"), headers=owner.headers, json={"status": "on_hold"}
    )
    assert response.status_code == 200, response.text
    row = await only(org, "job.status_changed", actor=owner, target_type="job", target_id=job.id)
    assert (row["metadata"]["old"], row["metadata"]["new"]) == ("planned", "on_hold")
    assert await changes(client, owner, org, "job", job.id, "job.status_changed") == [
        {"field": "status", "before": "planned", "after": "on_hold"}
    ]


async def test_changing_a_members_role_records_before_and_after(client):
    owner, org = await practice(client)
    person = await add_member(org["id"], "staff", full_name="Pat Person")
    response = await client.patch(
        org_url(org, f"/members/{person.id}"),
        headers=owner.headers,
        json={"role_id": await role_id(org["id"], "manager")},
    )
    assert response.status_code == 200, response.text
    row = await only(
        org, "member.role_changed", actor=owner, target_type="member", target_id=person.id
    )
    assert row["metadata"]["old"]["role_name"] == "Staff"
    assert row["metadata"]["new"]["role_name"] == "Manager"


# --- More of the same: records of every module ------------------------------------------------


async def test_time_jobs_and_disbursements_are_audited_from_creation_to_deletion(client):
    p = await new_practice(client)
    headers = p.owner.headers

    entry = await log_time(client, p.owner, p.org, client_id=p.client.id)
    url = p.url(f"/time-entries/{entry['id']}")
    assert (await client.patch(url, headers=headers, json={"minutes": 90})).status_code == 200
    assert (await client.delete(url, headers=headers)).status_code in (200, 204)
    for action in ("time_entry.created", "time_entry.updated", "time_entry.deleted"):
        await only(p.org, action, actor=p.owner, target_type="time_entry", target_id=entry["id"])

    made = await client.post(
        p.url("/jobs"), headers=headers, json={"client_id": str(p.client.id), "name": "Accounts"}
    )
    assert made.status_code == 201, made.text
    job_id = made.json()["data"]["id"]
    renamed = await client.patch(p.url(f"/jobs/{job_id}"), headers=headers, json={"name": "FY26"})
    assert renamed.status_code == 200, renamed.text
    assert (await client.delete(p.url(f"/jobs/{job_id}"), headers=headers)).status_code in (
        200,
        204,
    )
    for action in ("job.created", "job.updated", "job.deleted"):
        await only(p.org, action, actor=p.owner, target_type="job", target_id=job_id)
    assert await changes(client, p.owner, p.org, "job", job_id, "job.updated") == [
        {"field": "name", "before": "Accounts", "after": "FY26"}
    ]

    fee = await client.post(
        p.url("/disbursements"),
        headers=headers,
        json={
            "client_id": str(p.client.id),
            "incurred_on": "2026-09-10",
            "description": "Filing fee",
            "amount": 3400,
        },
    )
    assert fee.status_code == 201, fee.text
    fee_id = fee.json()["data"]["id"]
    url = p.url(f"/disbursements/{fee_id}")
    assert (await client.patch(url, headers=headers, json={"amount": 1300})).status_code == 200
    assert (await client.delete(url, headers=headers)).status_code == 200
    for action in ("disbursement.created", "disbursement.updated", "disbursement.deleted"):
        await only(p.org, action, actor=p.owner, target_type="disbursement", target_id=fee_id)
    assert await changes(
        client, p.owner, p.org, "disbursement", fee_id, "disbursement.updated"
    ) == [{"field": "amount", "before": 3400, "after": 1300}]


async def test_an_expense_claim_is_audited_through_submission_and_approval(client):
    _, org = await practice(client)
    person = await staff(org, full_name="Pat Person")
    manager = await staff(org, "manager")
    claim = await create_claim(client, person, org, lines=1)
    await act(client, person, org, claim["id"], "submit")
    await act(client, manager, org, claim["id"], "approve")

    for action, actor in (
        ("expense_claim.created", person),
        ("expense_claim.submitted", person),
        ("expense_claim.approved", manager),
    ):
        await only(org, action, actor=actor, target_type="expense_claim", target_id=claim["id"])
    await only(org, "expense.created", actor=person, target_type="expense")
    assert await changes(
        client, manager, org, "expense_claim", claim["id"], "expense_claim.approved"
    ) == [{"field": "status", "before": "submitted", "after": "approved"}]


async def test_voids_credits_and_reversals_are_audited(client):
    p = await new_practice(client)
    job = await hourly_job(p)
    wrong = await make_time_entry(p.org, p.owner, client=p.client, job=job)
    kept = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=30)
    dropped = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=15)
    headers = p.owner.headers

    voided = await issued(
        client,
        p.owner,
        p.org,
        (await draft(client, p.owner, p.org, p.client.id, time=[wrong]))["id"],
    )
    response = await client.post(
        p.url(f"/invoices/{voided['id']}/void"), headers=headers, json={"reason": "Wrong client"}
    )
    assert response.status_code == 200, response.text
    row = await only(
        p.org, "invoice.voided", actor=p.owner, target_type="invoice", target_id=voided["id"]
    )
    assert row["metadata"]["reason"] == "Wrong client"

    invoice = await issued(
        client,
        p.owner,
        p.org,
        (await draft(client, p.owner, p.org, p.client.id, time=[kept]))["id"],
    )
    credit = await client.post(
        p.url(f"/invoices/{invoice['id']}/credit-notes"),
        headers=headers,
        json={
            "reason": "Fee reduction",
            "lines": [{"description": "Reduction", "net_amount": 1_000}],
        },
    )
    assert credit.status_code == 201, credit.text
    await only(
        p.org,
        "credit_note.issued",
        actor=p.owner,
        target_type="credit_note",
        target_id=credit.json()["data"]["id"],
    )

    received = await pay(client, p.owner, p.org, p.client.id, 5_000)
    payment_id = received.json()["data"]["id"]
    refused = await client.post(
        p.url(f"/payments/{payment_id}/void"), headers=headers, json={"reason": "Bounced"}
    )
    assert refused.status_code == 200, refused.text
    await only(p.org, "payment.voided", actor=p.owner, target_type="payment", target_id=payment_id)

    off = await client.post(
        p.url("/wip/write-offs"),
        headers=headers,
        json={
            "client_id": str(p.client.id),
            "items": [{"time_entry_id": str(dropped.id)}],
            "reason": "Training",
        },
    )
    assert off.status_code == 201, off.text
    off_id = off.json()["data"]["id"]
    assert (
        await client.delete(p.url(f"/wip/write-offs/{off_id}"), headers=headers)
    ).status_code == 200
    await only(
        p.org,
        "wip.write_off_reversed",
        actor=p.owner,
        target_type="wip_write_off",
        target_id=off_id,
    )


async def test_staff_roles_and_settings_are_audited(client):
    owner, org = await practice(client)
    headers = owner.headers

    person = await new_staff(client, owner, org, "Sam Staff")
    paused = await client.post(
        org_url(org, f"/staff/{person['id']}/deactivate"),
        headers=headers,
        json={"status": "inactive"},
    )
    assert paused.status_code == 200, paused.text
    await only(org, "staff.created", actor=owner, target_type="member")
    await only(org, "staff.deactivated", actor=owner, target_type="member")

    made = await client.post(
        org_url(org, "/roles"),
        headers=headers,
        json={"name": "Reviewer", "permissions": ["client.read"]},
    )
    assert made.status_code == 201, made.text
    role_url = org_url(org, f"/roles/{made.json()['data']['id']}")
    granted = await client.put(
        f"{role_url}/permissions",
        headers=headers,
        json={"permissions": ["client.read", "job.read"]},
    )
    assert granted.status_code == 200, granted.text
    assert (await client.delete(role_url, headers=headers)).status_code in (200, 204)
    for action in ("role.created", "role.permissions_changed", "role.deleted"):
        await only(
            org, action, actor=owner, target_type="role", target_id=made.json()["data"]["id"]
        )

    rate = await client.post(
        org_url(org, "/vat-rates"),
        headers=headers,
        json={"name": "Flat 17.5%", "code": "standard", "rate_bp": 1750},
    )
    assert rate.status_code == 201, rate.text
    rate_id = rate.json()["data"]["id"]
    changed = await client.patch(
        org_url(org, f"/vat-rates/{rate_id}"), headers=headers, json={"rate_bp": 1800}
    )
    assert changed.status_code == 200, changed.text
    await only(org, "vat_rate.created", actor=owner, target_type="vat_rate", target_id=rate_id)
    await only(org, "vat_rate.updated", actor=owner, target_type="vat_rate", target_id=rate_id)
    assert await changes(client, owner, org, "vat_rate", rate_id, "vat_rate.updated") == [
        {"field": "rate_bp", "before": 1750, "after": 1800}
    ]


# --- The trail itself -------------------------------------------------------------------------


async def test_every_entry_of_these_flows_carries_an_actor_an_address_and_a_request(client):
    """A sweep over what the flows above wrote: nothing is anonymous or addressless."""
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, "Acme Ltd")
    await client.patch(
        org_url(org, f"/clients/{acme['id']}"), headers=owner.headers, json={"name": "Acme Group"}
    )
    await client.post(
        org_url(org, f"/clients/{acme['id']}/contacts"),
        headers=owner.headers,
        json={"name": "Jane", "email": "jane@example.com"},
    )
    t = today(org)
    await lock_through(client, owner, org, t - 30 * DAY)
    rows = await entry_rows(org)
    assert rows, "nothing was recorded"
    for row in rows:
        assert row["actor_user_id"] == str(owner.id), row["action"]
        assert row["ip"] == "127.0.0.1" and row["request_id"], row["action"]
        assert row["target_type"], row["action"]
    assert uuid.UUID(rows[0]["target_id"])
    assert (await client.get(audit_url(org), headers=owner.headers)).status_code == 200

"""Expense notifications: the approvers hear about a submission, the claimant about the decision;
who the approvers are, once per submission or decision, nobody notified about their own decision,
people who cannot be notified, the practice's and the person's preferences, and the emails."""

from tests.clients_helpers import sql
from tests.conftest import Mailbox, run_jobs
from tests.expense_helpers import act, create_claim, expense_team
from tests.factories import add_member
from tests.foundation_helpers import add_custom_role
from tests.notification_helpers import inbox, notification_jobs, put_own, put_practice
from tests.team_helpers import new_staff
from tests.time_helpers import set_manager

EXPENSE_TYPES = ("expense_pending", "expense_approved", "expense_returned")


async def expense_notes(client, actor, org) -> list[dict]:
    return [n for n in await inbox(client, actor, org) if n["type"] in EXPENSE_TYPES]


async def who_heard(client, team, *actors) -> dict[str, list[str]]:
    """The expense notification types in each person's inbox (those who heard nothing left out)."""
    heard = {}
    for name, actor in actors:
        types = [n["type"] for n in await expense_notes(client, actor, team.org)]
        if types:
            heard[name] = types
    return heard


async def everyone(team) -> list[tuple[str, object]]:
    roles = ("owner", "admin", "partner", "manager", "staff", "billing", "viewer")
    return [(role, await team.member(role)) for role in roles]


# --- A submission ---------------------------------------------------------------------------


async def test_a_submission_notifies_the_claimants_manager_and_nobody_else(client):
    team = await expense_team(client)
    org = team.org
    people = await everyone(team)  # everybody exists when the claim is submitted
    staff = await team.member("staff")
    manager = await team.member("manager")
    await set_manager(org, staff, manager)
    claim = await create_claim(client, staff, org, lines=1, title="Leeds trip")
    await act(client, staff, org, claim["id"], "submit")

    heard = await who_heard(client, team, *people)
    assert heard == {"manager": ["expense_pending"]}
    [note] = await expense_notes(client, manager, org)
    assert note["title"] == "Expense claim waiting for approval"
    assert note["link"] == "/expenses/approvals" and note["is_read"] is False
    assert note["entity_type"] == "expense_claim" and note["entity_id"] == claim["id"]
    assert staff.full_name in note["body"] and "Leeds trip" in note["body"]
    assert "10.00" in note["body"] and note["payload"] == {"claim_total": "£10.00"}
    # It queues an email, on by default, naming the type and carrying no address.
    [job] = await notification_jobs("email")
    assert job["type"] == "expense_pending" and "@" not in str(job)


async def test_without_a_manager_every_approver_who_reads_all_claims_is_notified(client):
    team = await expense_team(client)
    org = team.org
    await add_custom_role(
        org["id"],
        "Approver",
        ["organization.read", "member.read", "expense.log", "expense.approve"],
    )
    approver_only = await add_member(org["id"], "Approver")  # approves, but not everyone's
    people = [*await everyone(team), ("approver_only", approver_only)]
    staff = await team.member("staff")
    claim = await create_claim(client, staff, org, lines=1)
    await act(client, staff, org, claim["id"], "submit")
    heard = await who_heard(client, team, *people)
    # Approve and read everyone's claims: Owner, Admin, Partner and Manager; not the claimant,
    # Billing (reads, cannot approve), the Viewer or an approver of somebody else's staff.
    assert heard == {key: ["expense_pending"] for key in ("owner", "admin", "partner", "manager")}


async def test_a_manager_who_cannot_approve_does_not_stop_the_others_hearing(client):
    team = await expense_team(client)
    org = team.org
    people = await everyone(team)  # they must exist when the claim is submitted
    staff = await team.member("staff")
    billing = await team.member("billing")  # reads all, cannot approve
    await set_manager(org, staff, billing)
    claim = await create_claim(client, staff, org, lines=1)
    await act(client, staff, org, claim["id"], "submit")
    heard = await who_heard(client, team, *people)
    assert set(heard) == {"owner", "admin", "partner", "manager"}


async def test_a_claimant_who_is_an_approver_is_never_told_about_their_own_submission(client):
    team = await expense_team(client)
    org = team.org
    people = await everyone(team)
    manager = await team.member("manager")
    claim = await create_claim(client, manager, org, lines=1)
    await act(client, manager, org, claim["id"], "submit")
    heard = await who_heard(client, team, *people)
    assert set(heard) == {"owner", "admin", "partner"}  # not "manager", who submitted it
    # A practice of one has nobody to tell, and the submission still works.
    solo = await expense_team(client, "Solo Practice")
    own = await create_claim(client, solo.owner, solo.org, lines=1)
    done = await act(client, solo.owner, solo.org, own["id"], "submit")
    assert done["status"] == "submitted"
    assert await expense_notes(client, solo.owner, solo.org) == []


async def test_each_submission_is_notified_once_and_a_resubmission_again(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    manager = await team.member("manager")
    await set_manager(org, staff, manager)
    claim = await create_claim(client, staff, org, lines=1)
    cid = claim["id"]
    await act(client, staff, org, cid, "submit")
    assert len(await expense_notes(client, manager, org)) == 1
    await act(client, manager, org, cid, "return", json={"reason": "Redo"})
    await act(client, staff, org, cid, "submit")
    assert [n["type"] for n in await expense_notes(client, manager, org)] == [
        "expense_pending",
        "expense_pending",
    ]
    await act(client, staff, org, cid, "recall")
    await act(client, staff, org, cid, "submit")
    assert len(await expense_notes(client, manager, org)) == 3
    # Each submission queued its email, and the return queued one to the claimant.
    assert [j["type"] for j in await notification_jobs("email")] == [
        "expense_pending",
        "expense_returned",
        "expense_pending",
        "expense_pending",
    ]


# --- A decision -----------------------------------------------------------------------------


async def test_approving_notifies_the_claimant_and_emails_them(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    manager = await team.member("manager")
    await set_manager(org, staff, manager)
    claim = await create_claim(client, staff, org, lines=2, title="Conference")
    await act(client, staff, org, claim["id"], "submit")
    await act(client, manager, org, claim["id"], "approve", json={"note": "Thanks"})

    [note] = await expense_notes(client, staff, org)
    assert note["type"] == "expense_approved" and note["title"] == "Expense claim approved"
    assert note["link"] == "/expenses/mine" and note["entity_type"] == "expense_claim"
    assert note["entity_id"] == claim["id"] and note["payload"] == {"claim_total": "£20.00"}
    assert manager.full_name in note["body"] and "Conference" in note["body"]
    assert "20.00" in note["body"]
    # The decider heard nothing new; the claimant's email is queued with the manager's.
    assert [n["type"] for n in await expense_notes(client, manager, org)] == ["expense_pending"]
    assert [j["type"] for j in await notification_jobs("email")] == [
        "expense_pending",
        "expense_approved",
    ]


async def test_returning_notifies_the_claimant_with_the_reason_and_an_email(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    manager = await team.member("manager")
    await set_manager(org, staff, manager)
    claim = await create_claim(client, staff, org, lines=1)
    await act(client, staff, org, claim["id"], "submit")
    await act(
        client, manager, org, claim["id"], "return", json={"reason": "No receipt for the taxi"}
    )

    [note] = await expense_notes(client, staff, org)
    assert note["type"] == "expense_returned" and note["title"] == "Expense claim returned"
    assert note["link"] == "/expenses/mine" and note["entity_id"] == claim["id"]
    assert manager.full_name in note["body"] and "No receipt for the taxi" in note["body"]
    # An untitled claim is called by its generic name.
    assert "expense claim Expense claim (" in note["body"]
    assert sorted(j["type"] for j in await notification_jobs("email")) == [
        "expense_pending",
        "expense_returned",
    ]
    # Returned and approved again: each decision is its own notification.
    await act(client, staff, org, claim["id"], "submit")
    await act(client, manager, org, claim["id"], "approve")
    assert sorted(n["type"] for n in await expense_notes(client, staff, org)) == [
        "expense_approved",
        "expense_returned",
    ]


async def test_a_long_reason_is_clipped_in_the_notification_but_kept_whole_on_the_claim(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    claim = await create_claim(client, staff, org, lines=1)
    await act(client, staff, org, claim["id"], "submit")
    reason = "Please explain. " * 100
    returned = await act(client, team.owner, org, claim["id"], "return", json={"reason": reason})
    assert returned["decision_note"] == reason.strip()
    [note] = await expense_notes(client, staff, org)
    assert len(note["body"]) < len(reason) and "Please explain." in note["body"]


async def test_nobody_is_notified_about_their_own_decision(client):
    team = await expense_team(client)
    org = team.org
    own = await create_claim(client, team.owner, org, lines=1)
    await act(client, team.owner, org, own["id"], "submit")
    await act(client, team.owner, org, own["id"], "approve")
    other = await create_claim(client, team.owner, org, lines=1)
    await act(client, team.owner, org, other["id"], "submit")
    await act(client, team.owner, org, other["id"], "return", json={"reason": "Redo"})
    assert await expense_notes(client, team.owner, org) == []
    assert await notification_jobs("email") == []


async def test_a_bulk_approval_notifies_each_claimant_once(client):
    team = await expense_team(client)
    org = team.org
    ann, ben = await add_member(org["id"], "staff"), await add_member(org["id"], "staff")
    ids = []
    for claimant in (ann, ann, ben):
        claim = await create_claim(client, claimant, org, lines=1)
        ids.append((await act(client, claimant, org, claim["id"], "submit"))["id"])
    response = await client.post(
        team.url("/expense-approvals/bulk-approve"),
        headers=team.owner.headers,
        json={"claim_ids": ids},
    )
    assert response.status_code == 200
    assert sorted(n["entity_id"] for n in await expense_notes(client, ann, org)) == sorted(ids[:2])
    assert [n["entity_id"] for n in await expense_notes(client, ben, org)] == [ids[2]]
    # A bulk approval that fails halfway leaves no notification behind.
    claim = await create_claim(client, ann, org, lines=1)
    await act(client, ann, org, claim["id"], "submit")
    draft = await create_claim(client, ann, org, lines=1)
    before = len(await expense_notes(client, ann, org))
    failed = await client.post(
        team.url("/expense-approvals/bulk-approve"),
        headers=team.owner.headers,
        json={"claim_ids": [claim["id"], draft["id"]]},
    )
    assert failed.status_code == 409
    assert len(await expense_notes(client, ann, org)) == before


async def test_people_who_cannot_be_notified_are_skipped_without_failing_the_workflow(client):
    team = await expense_team(client)
    org = team.org
    admin = await team.member("admin")
    placeholder = await new_staff(client, team.owner, org, "No Login")
    claim = await create_claim(client, team.owner, org, lines=1, user_id=placeholder["id"])
    submitted = await act(client, team.owner, org, claim["id"], "submit")
    assert submitted["status"] == "submitted" and submitted["user_id"] == placeholder["id"]
    approved = await act(client, admin, org, claim["id"], "approve")
    assert approved["status"] == "approved"
    # The approvers heard about the submission; nobody could be told about the decision, as the
    # claimant has no login.
    rows = await sql("SELECT user_id, type FROM notifications ORDER BY created_at")
    assert [r["type"] for r in rows] == ["expense_pending"] * 2
    assert {str(r["user_id"]) for r in rows} == {str(team.owner.id), str(admin.id)}


# --- Preferences ----------------------------------------------------------------------------


async def test_the_practice_and_the_person_may_switch_the_notifications_off(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    manager = await team.member("manager")
    await set_manager(org, staff, manager)

    def switch(type_: str, **channels) -> dict:
        return {
            "types": [
                {
                    "type": type_,
                    "enabled": channels.pop("enabled", True),
                    "in_app": channels.pop("in_app", True),
                    "email": channels.pop("email", True),
                    "sms": False,
                }
            ]
        }

    # The practice turns the pending notification off.
    await put_practice(client, team.owner, org, switch("expense_pending", enabled=False))
    first = await create_claim(client, staff, org, lines=1)
    await act(client, staff, org, first["id"], "submit")
    assert await expense_notes(client, manager, org) == []
    assert await notification_jobs() == []
    # Back on: the manager hears again, but may keep it out of the inbox and the mailbox.
    await put_practice(client, team.owner, org, switch("expense_pending"))
    await put_own(
        client,
        manager,
        org,
        {
            "preferences": [
                {"type": "expense_pending", "in_app": False, "email": False, "sms": False}
            ]
        },
    )
    second = await create_claim(client, staff, org, lines=1)
    await act(client, staff, org, second["id"], "submit")
    assert await expense_notes(client, manager, org) == []
    assert await notification_jobs() == []
    # The claimant's own preference works the same way, and the decision still goes through.
    await put_own(
        client,
        staff,
        org,
        {
            "preferences": [
                {"type": "expense_approved", "in_app": False, "email": False, "sms": False}
            ]
        },
    )
    approved = await act(client, manager, org, second["id"], "approve")
    assert approved["status"] == "approved"
    assert await expense_notes(client, staff, org) == []


# --- The emails -----------------------------------------------------------------------------


async def test_the_emails_are_delivered_through_the_worker(client, worker, mailbox: Mailbox):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    manager = await team.member("manager")
    await set_manager(org, staff, manager)
    claim = await create_claim(client, staff, org, lines=1, title="Leeds trip")
    await act(client, staff, org, claim["id"], "submit")
    assert await run_jobs(worker) == 1

    [message] = mailbox.to(manager.email)
    assert message["Subject"] == f"{staff.full_name} submitted an expense claim for approval"
    text = Mailbox.text(message)
    assert "Leeds trip" in text and "£10.00" in text and staff.full_name in text
    assert f"/{org['slug']}/expenses/approvals" in text

    # The approval is emailed to the claimant.
    await act(client, manager, org, claim["id"], "approve")
    assert await run_jobs(worker) == 1
    [approved] = mailbox.to(staff.email)
    assert approved["Subject"] == "Your expense claim was approved"
    approved_text = Mailbox.text(approved)
    assert manager.full_name in approved_text and "Leeds trip" in approved_text
    assert f"/{org['slug']}/expenses/mine" in approved_text

    # A return is emailed too, with the reason.
    other = await create_claim(client, staff, org, lines=1, title="Taxi")
    await act(client, staff, org, other["id"], "submit")
    await act(client, manager, org, other["id"], "return", json={"reason": "Receipt missing"})
    assert await run_jobs(worker) == 2
    returned = mailbox.to(staff.email)[-1]
    assert returned["Subject"] == "Your expense claim was returned"
    returned_text = Mailbox.text(returned)
    assert "Reason: Receipt missing" in returned_text and "Taxi" in returned_text
    assert f"/{org['slug']}/expenses/mine" in returned_text
    assert len(mailbox.to(staff.email)) == 2  # the approval and the return
    assert len(mailbox.to(manager.email)) == 2  # the two submissions

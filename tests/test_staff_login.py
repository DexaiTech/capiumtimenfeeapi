"""Staff and login access: invitations (with the email in the mailbox), linking a new or an
existing login, unlinking, and the rules around them."""

from httpx import AsyncClient

from tests.clients_helpers import scalar, sql
from tests.conftest import Mailbox, run_jobs
from tests.domain_factories import make_client, make_job, practice
from tests.factories import API, add_member, audit_entries, create_user, error, role_id
from tests.foundation_helpers import create_person, sign_in
from tests.team_helpers import grade_id, new_rate, new_staff
from tests.time_helpers import MONDAY, log_time, ok_data, org_url

CHOSEN = "a brand new passphrase 42"


def staff_url(org: dict, path: str = "") -> str:
    return org_url(org, f"/staff{path}")


async def detail(client: AsyncClient, actor, org: dict, staff_id: str) -> dict:
    return ok_data(await client.get(staff_url(org, f"/{staff_id}"), headers=actor.headers))


async def choose_password(client: AsyncClient, token: str, password: str = CHOSEN) -> None:
    response = await client.post(
        f"{API}/auth/reset-password",
        json={"token": token, "password": password, "password_confirm": password},
    )
    assert response.status_code == 200, response.text


# --- Invitations ---------------------------------------------------------------------------------


async def test_an_invited_person_chooses_a_password_and_becomes_active(
    client: AsyncClient, worker, mailbox: Mailbox
) -> None:
    owner, org = await practice(client, "Acme Accountants")
    person = await new_staff(client, owner, org, "Jane Doe", email="jane@example.com")
    clean = await new_staff(client, owner, org, "Untouched")
    mailbox.clear()

    response = await client.post(
        staff_url(org, f"/{person['id']}/invite"), headers=owner.headers, json={}
    )
    data = ok_data(response)
    assert data["resent"] is False and data["expires_at"] > data["invited_at"]
    invited = data["staff"]
    assert invited["id"] == person["id"] and invited["code"] == person["code"]
    assert (invited["has_login"], invited["login_status"], invited["login_email"]) == (
        True,
        "invited",
        "jane@example.com",
    )
    assert invited["invited_at"] is not None and invited["role"]["system_key"] == "staff"
    assert (await detail(client, owner, org, clean["id"]))["login_status"] == "none"

    await run_jobs(worker)
    [message] = mailbox.to("jane@example.com")
    assert "invited" in message["Subject"].lower() and "Acme Accountants" in message["Subject"]
    body = Mailbox.text(message)
    assert "/reset-password?token=" in body and owner.full_name in body and "Jane Doe" in body

    # Nobody can sign in until the invitation is accepted.
    bad = await client.post(
        f"{API}/auth/login", json={"email": "jane@example.com", "password": CHOSEN}
    )
    assert bad.status_code == 401
    await choose_password(client, mailbox.token_for("jane@example.com"))
    jane = await sign_in(client, "jane@example.com", CHOSEN)
    assert (await detail(client, owner, org, person["id"]))["login_status"] == "active"
    # The same person (same id, code, history) now works in the practice with the role she was given.
    mine = ok_data(await client.get(f"{API}/users/me/organizations", headers=jane.headers))
    assert [o["organization"]["id"] for o in mine] == [org["id"]]
    assert str(jane.id) == person["id"]
    assert (await client.get(staff_url(org), headers=jane.headers)).status_code == 200
    [log] = await audit_entries(org["id"], "staff.invited")
    assert (
        log["metadata"]["login_email"] == "jane@example.com" and log["metadata"]["resent"] is False
    )
    # Accepted: inviting again is refused.
    again = await client.post(
        staff_url(org, f"/{person['id']}/invite"), headers=owner.headers, json={}
    )
    assert again.status_code == 409 and error(again)["code"] == "LOGIN_ALREADY_ACTIVE"


async def test_inviting_again_re_sends_a_fresh_link(
    client: AsyncClient, worker, mailbox: Mailbox
) -> None:
    owner, org = await practice(client)
    person = await new_staff(client, owner, org, "Jane Doe", email="jane@example.com")
    url = staff_url(org, f"/{person['id']}/invite")
    first = ok_data(await client.post(url, headers=owner.headers, json={}))
    await run_jobs(worker)
    old = mailbox.token_for("jane@example.com")
    second = ok_data(await client.post(url, headers=owner.headers, json={}))
    assert second["resent"] is True and second["staff"]["login_status"] == "invited"
    assert second["invited_at"] > first["invited_at"]
    await run_jobs(worker)
    new = mailbox.token_for("jane@example.com")
    assert new != old and len(mailbox.to("jane@example.com")) == 2
    stale = await client.post(
        f"{API}/auth/reset-password",
        json={"token": old, "password": CHOSEN, "password_confirm": CHOSEN},
    )
    assert stale.status_code >= 400
    await choose_password(client, new)
    assert (await sign_in(client, "jane@example.com", CHOSEN)).id


async def test_invitation_rules(client: AsyncClient) -> None:
    owner, org = await practice(client)
    admin = await add_member(org["id"], "admin")
    nobody = await new_staff(client, owner, org, "No email")

    def url(staff_id: str) -> str:
        return staff_url(org, f"/{staff_id}/invite")

    response = await client.post(url(nobody["id"]), headers=owner.headers, json={})
    assert response.status_code == 422 and error(response)["details"][0]["field"] == "email"
    # An address that is already an account, or another staff member's work email.
    response = await client.post(
        url(nobody["id"]), headers=owner.headers, json={"email": owner.email}
    )
    assert response.status_code == 409 and error(response)["code"] == "EMAIL_TAKEN"
    await new_staff(client, owner, org, "Holder", email="held@example.com")
    response = await client.post(
        url(nobody["id"]), headers=owner.headers, json={"email": "HELD@example.com"}
    )
    assert response.status_code == 409 and error(response)["code"] in (
        "STAFF_EMAIL_TAKEN",
        "EMAIL_TAKEN",
    )
    # Someone who can already sign in.
    response = await client.post(url(str(owner.id)), headers=owner.headers, json={})
    assert response.status_code == 409 and error(response)["code"] == "LOGIN_ALREADY_ACTIVE"
    # Only active staff.
    await client.post(staff_url(org, f"/{nobody['id']}/deactivate"), headers=owner.headers, json={})
    response = await client.post(
        url(nobody["id"]), headers=owner.headers, json={"email": "x@example.com"}
    )
    assert response.status_code == 409 and error(response)["code"] == "STAFF_NOT_ACTIVE"
    await client.post(staff_url(org, f"/{nobody['id']}/reactivate"), headers=owner.headers)
    # The role is handed out under the usual rules: an admin cannot make an owner, nobody a role that does not exist.
    owner_role = await role_id(org["id"], "owner")
    response = await client.post(
        url(nobody["id"]),
        headers=admin.headers,
        json={"email": "x@example.com", "role_id": owner_role},
    )
    assert response.status_code == 403
    response = await client.post(
        url(nobody["id"]),
        headers=owner.headers,
        json={"email": "x@example.com", "role_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code == 422
    response = await client.post(
        url(nobody["id"]), headers=owner.headers, json={"email": "not-an-email"}
    )
    assert response.status_code == 422
    assert (await detail(client, owner, org, nobody["id"]))["login_status"] == "none"
    ok = await client.post(
        url(nobody["id"]),
        headers=owner.headers,
        json={"email": "x@example.com", "role_id": await role_id(org["id"], "manager")},
    )
    assert ok_data(ok)["staff"]["role"]["system_key"] == "manager"


# --- Link: new account ------------------------------------------------------------------------------


async def test_link_a_new_account_with_a_temporary_password(
    client: AsyncClient, worker, mailbox: Mailbox
) -> None:
    owner, org = await practice(client)
    person = await new_staff(client, owner, org, "Jane Doe", email="jane@example.com")
    mailbox.clear()
    response = await client.post(
        staff_url(org, f"/{person['id']}/link-login"),
        headers=owner.headers,
        json={"mode": "create_account", "role_id": await role_id(org["id"], "manager")},
    )
    data = ok_data(response)
    assert response.headers["cache-control"] == "no-store"
    assert data["merged_from"] is None and data["email_sent"] is True and data["temporary_password"]
    linked = data["staff"]
    assert (
        linked["has_login"],
        linked["login_status"],
        linked["login_email"],
        linked["role"]["system_key"],
    ) == (True, "active", "jane@example.com", "manager")
    assert linked["id"] == person["id"] and linked["code"] == person["code"]

    # She signs in with the temporary password and has to change it; the email carries a link, not the password.
    jane = await sign_in(client, "jane@example.com", data["temporary_password"])
    blocked = await client.get(f"{API}/users/me/organizations", headers=jane.headers)
    assert blocked.status_code == 403 and error(blocked)["code"] == "PASSWORD_CHANGE_REQUIRED"
    await run_jobs(worker)
    [message] = mailbox.to("jane@example.com")
    assert message["Subject"].endswith("account is ready")
    assert data["temporary_password"] not in Mailbox.text(message)
    [log] = await audit_entries(org["id"], "staff.login_linked")
    assert log["metadata"]["mode"] == "create_account" and data["temporary_password"] not in str(
        log
    )


async def test_link_a_new_account_without_email_and_with_a_chosen_address(
    client: AsyncClient, worker, mailbox: Mailbox
) -> None:
    owner, org = await practice(client)
    person = await new_staff(client, owner, org, "Jane Doe")
    mailbox.clear()
    response = await client.post(
        staff_url(org, f"/{person['id']}/link-login"),
        headers=owner.headers,
        json={"mode": "create_account", "email": "jane.login@example.com", "send_email": False},
    )
    data = ok_data(response)
    assert data["email_sent"] is False and data["staff"]["login_email"] == "jane.login@example.com"
    assert data["staff"]["email"] == "jane.login@example.com"  # becomes the work email too
    await run_jobs(worker)
    assert mailbox.messages == []


async def test_link_account_rules(client: AsyncClient) -> None:
    owner, org = await practice(client)
    admin = await add_member(org["id"], "admin")
    nobody = await new_staff(client, owner, org, "No email")

    async def link(staff_id: str, **body):
        return await client.post(
            staff_url(org, f"/{staff_id}/link-login"), headers=owner.headers, json=body
        )

    response = await link(nobody["id"], mode="create_account")
    assert response.status_code == 422 and error(response)["details"][0]["field"] == "email"
    response = await link(nobody["id"], mode="create_account", email=owner.email)
    assert response.status_code == 409 and error(response)["code"] == "EMAIL_TAKEN"
    await new_staff(client, owner, org, "Holder", email="held@example.com")
    response = await link(nobody["id"], mode="create_account", email="held@example.com")
    assert response.status_code == 409 and error(response)["code"] in (
        "STAFF_EMAIL_TAKEN",
        "EMAIL_TAKEN",
    )
    response = await link(str(owner.id), mode="create_account", email="x@example.com")
    assert response.status_code == 409 and error(response)["code"] == "LOGIN_ALREADY_LINKED"
    for body in (
        {"mode": "nonsense"},
        {},
        {"mode": "existing_user"},
        {"mode": "create_account", "email": "bad"},
    ):
        assert (await link(nobody["id"], **body)).status_code == 422, body
    owner_role = await role_id(org["id"], "owner")
    response = await client.post(
        staff_url(org, f"/{nobody['id']}/link-login"),
        headers=admin.headers,
        json={"mode": "create_account", "email": "x@example.com", "role_id": owner_role},
    )
    assert response.status_code == 403
    await client.post(staff_url(org, f"/{nobody['id']}/deactivate"), headers=owner.headers, json={})
    response = await link(nobody["id"], mode="create_account", email="x@example.com")
    assert response.status_code == 409 and error(response)["code"] == "STAFF_NOT_ACTIVE"
    assert (await detail(client, owner, org, nobody["id"]))["has_login"] is False


# --- Link: existing login (merge) -----------------------------------------------------------------------


async def test_linking_an_existing_login_merges_the_records(client: AsyncClient) -> None:
    owner, org = await practice(client)
    created = await create_person(
        client, owner, org, email="jane@example.com", full_name="Jane Login"
    )
    login_id = created["member"]["user_id"]
    # The staff record carries the profile; both have history.
    manager = await add_member(org["id"], "manager")
    record = await new_staff(
        client,
        owner,
        org,
        "Jane Record",
        code="JANE-1",
        email="jane.work@example.com",
        grade_id=await grade_id(org, "MGR"),
        start_date="2026-02-02",
        working_days=[1, 2, 3],
        working_minutes_per_day=400,
        chargeable_target_bp=6_000,
        approver_id=str(manager.id),
        job_title="Senior",
        initials="JR",
    )
    acme = await make_client(org)
    entry = await log_time(client, owner, org, user_id=record["id"], client_id=acme.id)
    await new_rate(
        client,
        owner,
        org,
        kind="charge",
        user_id=record["id"],
        rate=4_400,
        effective_from="2026-01-01",
    )
    reports = await new_staff(client, owner, org, "Reports")
    await sql(
        "UPDATE organization_members SET manager_user_id = :m WHERE organization_id = :o AND user_id = :u",
        m=record["id"],
        o=org["id"],
        u=reports["id"],
    )
    job = await make_job(org, acme)
    await sql(
        "INSERT INTO job_assignees (organization_id, job_id, user_id) VALUES (:o, :j, :u)",
        o=org["id"],
        j=str(job.id),
        u=record["id"],
    )

    response = await client.post(
        staff_url(org, f"/{record['id']}/link-login"),
        headers=owner.headers,
        json={"mode": "existing_user", "email": "JANE@example.com"},
    )
    data = ok_data(response)
    assert (
        data["merged_from"] == record["id"]
        and data["temporary_password"] is None
        and data["email_sent"] is False
    )
    merged = data["staff"]
    # The login member survives and takes over the staff profile.
    assert merged["id"] == login_id and merged["full_name"] == "Jane Login"
    assert (merged["code"], merged["email"], merged["grade"]["code"], merged["start_date"]) == (
        "JANE-1",
        "jane.work@example.com",
        "MGR",
        "2026-02-02",
    )
    assert (
        merged["working_days"],
        merged["working_minutes_per_day"],
        merged["weekly_capacity_minutes"],
        merged["chargeable_target_bp"],
    ) == ([1, 2, 3], 400, 1_200, 6_000)
    assert (merged["job_title"], merged["initials"]) == ("Senior", "JR")
    assert merged["approver"]["id"] == str(manager.id) and merged["has_login"] is True
    assert merged["role"]["system_key"] == "staff"  # the login keeps its role

    # Everything recorded for the placeholder moved to the login member; the placeholder is gone.
    assert (
        await client.get(staff_url(org, f"/{record['id']}"), headers=owner.headers)
    ).status_code == 404
    assert await scalar("SELECT count(*) FROM users WHERE id = :i", i=record["id"]) == 0
    entries = ok_data(
        await client.get(
            org_url(org, "/time-entries"), headers=owner.headers, params={"user_id": login_id}
        )
    )
    assert [e["id"] for e in entries] == [entry["id"]]
    rules = ok_data(
        await client.get(
            org_url(org, "/rates"), headers=owner.headers, params={"user_id": login_id}
        )
    )
    assert [r["rate"] for r in rules] == [4_400]
    assert (await detail(client, owner, org, reports["id"]))["approver"]["id"] == login_id
    assert await scalar("SELECT count(*) FROM job_assignees WHERE user_id = :u", u=login_id) == 1
    assert len(ok_data(await client.get(staff_url(org), headers=owner.headers))) == 4
    [log] = await audit_entries(org["id"], "staff.login_linked")
    assert (
        log["metadata"]["mode"] == "existing_user"
        and log["metadata"]["merged_from"] == record["id"]
    )
    assert log["metadata"]["moved"]["time_entries"] == 1 and log["target_id"] == login_id


async def test_merge_conflicts_change_nothing(client: AsyncClient) -> None:
    owner, org = await practice(client)
    created = await create_person(
        client, owner, org, email="jane@example.com", full_name="Jane Login"
    )
    login_id = created["member"]["user_id"]
    record = await new_staff(client, owner, org, "Jane Record", code="JANE-1")
    acme = await make_client(org)
    # Both have a timesheet for the same week: they cannot be combined.
    await log_time(
        client, owner, org, user_id=record["id"], client_id=acme.id, entry_date=MONDAY.isoformat()
    )
    await log_time(
        client, owner, org, user_id=login_id, client_id=acme.id, entry_date=MONDAY.isoformat()
    )
    response = await client.post(
        staff_url(org, f"/{record['id']}/link-login"),
        headers=owner.headers,
        json={"mode": "existing_user", "email": "jane@example.com"},
    )
    assert response.status_code == 409 and error(response)["code"] == "LINK_CONFLICT"
    assert error(response)["details"]["table"] == "timesheets"
    assert (await detail(client, owner, org, record["id"]))["has_login"] is False
    assert (await detail(client, owner, org, login_id))["code"] != "JANE-1"
    assert (
        len(
            ok_data(
                await client.get(
                    org_url(org, "/time-entries"),
                    headers=owner.headers,
                    params={"user_id": record["id"]},
                )
            )
        )
        == 1
    )
    assert (
        len(
            ok_data(
                await client.get(
                    org_url(org, "/time-entries"),
                    headers=owner.headers,
                    params={"user_id": login_id},
                )
            )
        )
        == 1
    )
    assert await audit_entries(org["id"], "staff.login_linked") == []


async def test_merge_needs_an_active_login_member_of_this_practice(client: AsyncClient) -> None:
    owner, org = await practice(client)
    _, other = await practice(client, "Other & Co")
    await create_user("Outsider", "outsider@example.com")
    await add_member(
        org["id"], "staff", full_name="Sus", email="sus@example.com", status="suspended"
    )
    await new_staff(client, owner, org, "Placeholder", email="ph@example.com")
    record = await new_staff(client, owner, org, "Record")
    await add_member(other["id"], "staff", full_name="Theirs", email="theirs@example.com")
    for email in (
        "outsider@example.com",  # not a member of the practice
        "sus@example.com",  # not active
        "ph@example.com",  # a work email, not a login
        "theirs@example.com",  # a member of another practice
        "nobody@example.com",
    ):
        response = await client.post(
            staff_url(org, f"/{record['id']}/link-login"),
            headers=owner.headers,
            json={"mode": "existing_user", "email": email},
        )
        assert response.status_code == 422, (email, response.text)
    assert (await detail(client, owner, org, record["id"]))["has_login"] is False


# --- Unlink -----------------------------------------------------------------------------------------------


async def test_unlinking_a_login_keeps_the_person_and_their_history(client: AsyncClient) -> None:
    owner, org = await practice(client)
    created = await create_person(
        client, owner, org, email="jane@example.com", full_name="Jane Login"
    )
    jane = await sign_in(client, "jane@example.com", created["temporary_password"])
    # She has time recorded and approves someone's timesheets.
    acme = await make_client(org)
    await log_time(client, owner, org, user_id=created["member"]["user_id"], client_id=acme.id)
    report = await new_staff(
        client, owner, org, "Reports", approver_id=created["member"]["user_id"]
    )
    login_id = created["member"]["user_id"]

    response = await client.post(staff_url(org, f"/{login_id}/unlink-login"), headers=owner.headers)
    unlinked = ok_data(response)
    assert (
        unlinked["has_login"],
        unlinked["login_status"],
        unlinked["login_email"],
        unlinked["invited_at"],
    ) == (False, "none", None, None)
    assert unlinked["email"] == "jane@example.com"  # kept as the work email
    assert unlinked["status"] == "active" and unlinked["usage"]
    # No way in any more: sessions are gone and the account is a placeholder.
    assert (await client.get(staff_url(org), headers=jane.headers)).status_code == 401
    bad = await client.post(
        f"{API}/auth/login",
        json={"email": "jane@example.com", "password": created["temporary_password"]},
    )
    assert bad.status_code == 401
    user = (await sql("SELECT email, is_active FROM users WHERE id = :i", i=login_id))[0]
    assert user["is_active"] is False and user["email"].endswith("@no-login.invalid")
    # History stays, and the people she approved lose their approver.
    assert (
        len(
            ok_data(
                await client.get(
                    org_url(org, "/time-entries"),
                    headers=owner.headers,
                    params={"user_id": login_id},
                )
            )
        )
        == 1
    )
    assert (await detail(client, owner, org, report["id"]))["approver"] is None
    [log] = await audit_entries(org["id"], "staff.login_unlinked")
    assert (
        log["metadata"]["login_email"] == "jane@example.com"
        and log["metadata"]["approver_links_cleared"] == 1
    )
    again = await client.post(staff_url(org, f"/{login_id}/unlink-login"), headers=owner.headers)
    assert again.status_code == 409 and error(again)["code"] == "NO_LOGIN"
    # And access can be given again.
    invited = await client.post(
        staff_url(org, f"/{login_id}/invite"), headers=owner.headers, json={}
    )
    assert ok_data(invited)["staff"]["login_status"] == "invited"


async def test_a_shared_account_cannot_be_unlinked_or_renamed_by_one_practice(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    _, other = await practice(client, "Other & Co")
    shared = await add_member(
        org["id"], "staff", full_name="Shared Sam", email="shared@example.com"
    )
    await add_member(other["id"], "staff", user=shared)
    response = await client.post(
        staff_url(org, f"/{shared.id}/unlink-login"), headers=owner.headers
    )
    assert response.status_code == 409 and error(response)["code"] == "ACCOUNT_SHARED"
    response = await client.patch(
        staff_url(org, f"/{shared.id}"), headers=owner.headers, json={"full_name": "Renamed"}
    )
    assert response.status_code == 409 and error(response)["code"] == "ACCOUNT_SHARED"
    assert (
        await client.patch(
            staff_url(org, f"/{shared.id}"), headers=owner.headers, json={"job_title": "Fine"}
        )
    ).status_code == 200
    # Leaving one practice does not sign them out of the other.
    left = await client.post(
        staff_url(org, f"/{shared.id}/deactivate"), headers=owner.headers, json={"status": "left"}
    )
    assert left.status_code == 200
    assert (await client.get(f"{API}/users/me", headers=shared.headers)).status_code == 200


async def test_placeholder_accounts_cannot_sign_in(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await new_staff(client, owner, org, "No Login")
    for password in (CHOSEN, "correct horse battery"):
        response = await client.post(
            f"{API}/auth/login",
            json={"email": f"staff+{person['id']}@no-login.invalid", "password": password},
        )
        assert response.status_code in (401, 422)

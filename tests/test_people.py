"""POST {org}/people and POST {org}/members/{user_id}/reset-password."""

import pytest
from sqlalchemy import text

from app.repositories.users import UserRepository
from tests.factories import (
    API,
    add_member,
    audit_entries,
    create_org,
    create_user,
    create_user_with_org,
    error,
    role_id,
)
from tests.foundation_helpers import create_person, people_url, sign_in

ALPHABET = set("abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789")


def reset_url(org: dict, user_id) -> str:
    return f"{API}/organizations/{org['id']}/members/{user_id}/reset-password"


async def test_create_returns_member_and_a_generated_password_once(client):
    owner, org = await create_user_with_org(client)
    response = await client.post(
        people_url(org),
        headers=owner.headers,
        json={
            "full_name": "Grace Hopper",
            "email": "Grace@Example.com",
            "role_id": await role_id(org["id"], "manager"),
            "job_title": "Engineer",
        },
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    password = data["temporary_password"]
    assert len(password) == 16 and set(password) <= ALPHABET
    member = data["member"]
    assert member["full_name"] == "Grace Hopper" and member["email"] == "Grace@example.com"
    assert member["role"]["system_key"] == "manager" and member["status"] == "active"
    assert member["job_title"] == "Engineer" and member["must_change_password"] is True

    # The account is verified, so the first sign-in works; the user flag is exposed.
    signed_in = await client.post(
        f"{API}/auth/login", json={"email": "grace@example.com", "password": password}
    )
    assert signed_in.status_code == 200
    user = signed_in.json()["data"]["user"]
    assert user["email_verified"] is True and user["must_change_password"] is True


async def test_each_password_is_different_and_never_stored_in_clear(client, db):
    owner, org = await create_user_with_org(client)
    first = await create_person(client, owner, org, email="a1@example.com")
    second = await create_person(client, owner, org, email="a2@example.com")
    assert first["temporary_password"] != second["temporary_password"]
    hashes = (
        await db.execute(text("SELECT password_hash FROM users WHERE email LIKE 'a_@%'"))
    ).all()
    assert len(hashes) == 2
    assert all(first["temporary_password"] not in h[0] for h in hashes)
    assert all(h[0].startswith("$argon2") for h in hashes)


async def test_create_is_audited_without_the_password(client):
    owner, org = await create_user_with_org(client)
    data = await create_person(client, owner, org, email="audit@example.com")
    [entry] = await audit_entries(org["id"], "member.created")
    assert entry["actor_user_id"] == str(owner.id)
    assert entry["target_type"] == "member" and entry["target_id"] == data["member"]["user_id"]
    assert entry["metadata"]["member_email"] == "audit@example.com"
    assert data["temporary_password"] not in str(entry)
    # Nothing else that was written holds it either.
    assert all(data["temporary_password"] not in str(e) for e in await audit_entries(org["id"]))


async def test_create_with_manager_initials_and_capacity(client, db):
    owner, org = await create_user_with_org(client)
    lead = await add_member(org["id"], "manager")
    data = await create_person(
        client,
        owner,
        org,
        initials=" gh ",
        manager_user_id=str(lead.id),
        weekly_capacity_minutes=1800,
        job_title="Senior",
    )
    member = data["member"]
    assert member["initials"] == "GH" and member["manager_user_id"] == str(lead.id)
    assert member["weekly_capacity_minutes"] == 1800 and member["job_title"] == "Senior"
    assert member["chargeable_target_bp"] == 7500
    row = (
        await db.execute(
            text(
                "SELECT initials, manager_user_id, weekly_capacity_minutes "
                "FROM organization_members WHERE user_id = :id"
            ),
            {"id": member["user_id"]},
        )
    ).one()
    assert (row.initials, str(row.manager_user_id), row.weekly_capacity_minutes) == (
        "GH",
        str(lead.id),
        1800,
    )
    # Without them: no initials, no manager, the default 37.5 hour week.
    plain = (await create_person(client, owner, org, email="plain@example.com"))["member"]
    assert plain["initials"] is None and plain["manager_user_id"] is None
    assert plain["weekly_capacity_minutes"] == 2250
    blank = await create_person(client, owner, org, email="blank@example.com", initials="")
    assert blank["member"]["initials"] is None


async def test_create_rejects_initials_already_used_in_the_practice(client, db):
    owner, org = await create_user_with_org(client)
    await create_person(client, owner, org, email="first@example.com", initials="AB")
    response = await client.post(
        people_url(org),
        headers=owner.headers,
        json={
            "full_name": "Second",
            "email": "second@example.com",
            "role_id": await role_id(org["id"], "staff"),
            "initials": "ab",
        },
    )
    assert response.status_code == 409, response.text
    assert error(response)["code"] == "INITIALS_TAKEN"
    assert error(response)["details"][0]["field"] == "initials"
    count = await db.scalar(text("SELECT count(*) FROM users WHERE email = 'second@example.com'"))
    assert count == 0
    # Other practices may use the same initials.
    other_owner, other = await create_user_with_org(client, "Other")
    await create_person(client, other_owner, other, email="third@example.com", initials="AB")


@pytest.mark.parametrize(
    "manager",
    ["suspended", "removed", "foreign", "unknown"],
)
async def test_create_needs_an_active_manager_of_the_same_practice(client, db, manager):
    owner, org = await create_user_with_org(client)
    _, other = await create_user_with_org(client, "Other")
    managers = {
        "suspended": lambda: add_member(org["id"], "manager", status="suspended"),
        "removed": lambda: add_member(org["id"], "manager", status="removed"),
        "foreign": lambda: add_member(other["id"], "manager"),
    }
    manager_id = (
        str((await managers[manager]()).id)
        if manager in managers
        else "01a11108-f0b8-73b2-8dd0-19f38e068d0c"
    )
    response = await client.post(
        people_url(org),
        headers=owner.headers,
        json={
            "full_name": "Ada",
            "email": "managed@example.com",
            "role_id": await role_id(org["id"], "staff"),
            "manager_user_id": manager_id,
        },
    )
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == "manager_user_id"
    count = await db.scalar(text("SELECT count(*) FROM users WHERE email = 'managed@example.com'"))
    assert count == 0


async def test_duplicate_email_is_409_email_taken_in_any_case(client):
    owner, org = await create_user_with_org(client)
    await create_person(client, owner, org, email="dup@example.com")
    body = {"full_name": "X", "role_id": await role_id(org["id"], "staff")}
    for email in ("dup@example.com", "DUP@example.com", owner.email.upper()):
        response = await client.post(
            people_url(org), headers=owner.headers, json={**body, "email": email}
        )
        assert response.status_code == 409, email
        assert error(response)["code"] == "EMAIL_TAKEN"


async def test_an_email_that_belongs_to_a_user_of_another_organization_is_taken(client):
    owner, org = await create_user_with_org(client)
    other_owner, _ = await create_user_with_org(client, name="Other")
    response = await client.post(
        people_url(org),
        headers=owner.headers,
        json={
            "full_name": "X",
            "email": other_owner.email,
            "role_id": await role_id(org["id"], "staff"),
        },
    )
    assert response.status_code == 409 and error(response)["code"] == "EMAIL_TAKEN"


async def test_losing_the_email_race_to_another_practice_is_still_email_taken(
    client, db, monkeypatch
):
    owner, org = await create_user_with_org(client)
    other_owner, _ = await create_user_with_org(client, name="Other")

    async def not_seen(self, email):
        # The other practice's request committed after this one looked the address up.
        return None

    monkeypatch.setattr(UserRepository, "get_by_email", not_seen)
    response = await client.post(
        people_url(org),
        headers=owner.headers,
        json={
            "full_name": "X",
            "email": other_owner.email.upper(),
            "role_id": await role_id(org["id"], "staff"),
            "send_email": False,
        },
    )
    assert response.status_code == 409, response.text
    body = error(response)
    assert body["code"] == "EMAIL_TAKEN" and body["details"][0]["field"] == "email"
    members = await db.scalar(
        text("SELECT count(*) FROM organization_members WHERE organization_id = :org"),
        {"org": org["id"]},
    )
    assert members == 1
    assert await audit_entries(org["id"], "member.created") == []


@pytest.mark.parametrize(
    "patch",
    [
        {"full_name": ""},
        {"full_name": "x" * 121},
        {"email": "not-an-email"},
        {"role_id": "nope"},
        {"job_title": "x" * 121},
        {"team_ids": []},
        {"initials": "TOOLONG"},
        {"initials": "A-B"},
        {"manager_user_id": "nope"},
        {"weekly_capacity_minutes": -1},
        {"weekly_capacity_minutes": 10081},
        {"weekly_capacity_minutes": None},
        {"send_email": "perhaps"},
        {"extra": 1},
    ],
)
async def test_validation(client, patch):
    owner, org = await create_user_with_org(client)
    body = {
        "full_name": "Ada",
        "email": "ada2@example.com",
        "role_id": await role_id(org["id"], "staff"),
        **patch,
    }
    response = await client.post(people_url(org), headers=owner.headers, json=body)
    assert response.status_code == 422 and error(response)["code"] == "VALIDATION_ERROR"


async def test_unknown_or_foreign_role_is_422_and_nothing_is_created(client, db):
    owner, org = await create_user_with_org(client)
    _other_owner, other_org = await create_user_with_org(client, name="Other")
    body = {"full_name": "Ada", "email": "ada3@example.com"}
    for role in (await role_id(other_org["id"], "staff"), "01a11108-f0b8-73b2-8dd0-19f38e068d0c"):
        response = await client.post(
            people_url(org), headers=owner.headers, json={**body, "role_id": role}
        )
        assert response.status_code == 422
        assert error(response)["details"][0]["field"] == "role_id"
    assert await db.scalar(text("SELECT count(*) FROM users WHERE email = 'ada3@example.com'")) == 0


async def test_requires_member_invite(client):
    _owner, org = await create_user_with_org(client)
    viewer_role = await role_id(org["id"], "viewer")
    for key in ("manager", "staff", "billing", "viewer"):
        member = await add_member(org["id"], key)
        response = await client.post(
            people_url(org),
            headers=member.headers,
            json={"full_name": "A", "email": f"{key}@example.com", "role_id": viewer_role},
        )
        assert response.status_code == 403, key
        assert error(response)["code"] == "PERMISSION_DENIED"
    for key in ("admin", "partner"):
        actor = await add_member(org["id"], key)
        await create_person(client, actor, org, role="viewer", email=f"by-{key}@example.com")
    assert (await client.post(people_url(org), json={})).status_code == 401


async def test_nobody_grants_a_role_they_lack_and_only_owners_make_owners(client):
    owner, org = await create_user_with_org(client)
    admin = await add_member(org["id"], "admin")
    owner_role = await role_id(org["id"], "owner")
    response = await client.post(
        people_url(org),
        headers=admin.headers,
        json={"full_name": "Boss", "email": "boss@example.com", "role_id": owner_role},
    )
    assert response.status_code == 403
    # An Admin can create people in roles they cover (here: Manager).
    ok = await create_person(client, admin, org, role="manager", email="mgr@example.com")
    assert ok["member"]["role"]["system_key"] == "manager"
    # The Owner can create another Owner.
    second = await create_person(client, owner, org, role="owner", email="owner2@example.com")
    assert second["member"]["role"]["system_key"] == "owner"


async def test_cross_tenant_organization_is_404(client):
    _owner, org = await create_user_with_org(client)
    outsider, _ = await create_user_with_org(client, name="Other")
    response = await client.post(
        people_url(org),
        headers=outsider.headers,
        json={
            "full_name": "A",
            "email": "a@example.com",
            "role_id": await role_id(org["id"], "staff"),
        },
    )
    assert response.status_code == 404


async def test_reset_password_gives_a_new_temporary_password_and_ends_sessions(client):
    owner, org = await create_user_with_org(client)
    created = await create_person(client, owner, org, email="reset@example.com")
    old = created["temporary_password"]
    person = await sign_in(client, "reset@example.com", old)
    assert (await client.get(f"{API}/users/me", headers=person.headers)).status_code == 200

    response = await client.post(reset_url(org, person.id), headers=owner.headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    new = response.json()["data"]["temporary_password"]
    assert new != old and len(new) == 16

    # The old session and token are dead; the old password no longer works.
    assert (await client.get(f"{API}/users/me", headers=person.headers)).status_code == 401
    assert (
        await client.post(f"{API}/auth/login", json={"email": "reset@example.com", "password": old})
    ).status_code == 401
    again = await client.post(
        f"{API}/auth/login", json={"email": "reset@example.com", "password": new}
    )
    assert again.status_code == 200 and again.json()["data"]["user"]["must_change_password"] is True

    [entry] = await audit_entries(org["id"], "member.password_reset")
    assert entry["target_id"] == str(person.id) and entry["actor_user_id"] == str(owner.id)
    assert new not in str(await audit_entries(org["id"]))


async def test_reset_password_sets_the_flag_again_after_a_change(client):
    owner, org = await create_user_with_org(client)
    created = await create_person(client, owner, org, email="again@example.com")
    person = await sign_in(client, "again@example.com", created["temporary_password"])
    changed = await client.post(
        f"{API}/users/me/password",
        headers=person.headers,
        json={
            "current_password": created["temporary_password"],
            "password": "a brand new secret",
            "password_confirm": "a brand new secret",
        },
    )
    assert changed.status_code == 200
    me = await client.get(f"{API}/users/me", headers=person.headers)
    assert me.json()["data"]["must_change_password"] is False
    await client.post(reset_url(org, person.id), headers=owner.headers)
    login = await client.post(
        f"{API}/auth/login", json={"email": "again@example.com", "password": "a brand new secret"}
    )
    assert login.status_code == 401


async def test_reset_password_rules(client):
    owner, org = await create_user_with_org(client)
    admin = await add_member(org["id"], "admin")
    member = await add_member(org["id"], "staff")
    other = await add_member(org["id"], "staff")

    own = await client.post(reset_url(org, owner.id), headers=owner.headers)
    assert own.status_code == 409 and error(own)["code"] == "USE_CHANGE_PASSWORD"
    # Needs member.update.
    assert (await client.post(reset_url(org, other.id), headers=member.headers)).status_code == 403
    # An Admin cannot take over an Owner's account; an Owner can reset an Admin.
    assert (await client.post(reset_url(org, owner.id), headers=admin.headers)).status_code == 403
    assert (await client.post(reset_url(org, admin.id), headers=owner.headers)).status_code == 200
    # Unknown, removed and foreign people are 404.
    removed = await add_member(org["id"], "staff", status="removed")
    for user_id in (removed.id, "01a11108-f0b8-73b2-8dd0-19f38e068d0c"):
        assert (
            await client.post(reset_url(org, user_id), headers=owner.headers)
        ).status_code == 404
    _, org_b = await create_user_with_org(client, name="Other")
    stranger = await add_member(org_b["id"], "staff")
    assert (
        await client.post(reset_url(org, stranger.id), headers=owner.headers)
    ).status_code == 404
    other_owner = await create_user()
    assert (
        await client.post(reset_url(org, other.id), headers=other_owner.headers)
    ).status_code == 404


async def test_reset_is_refused_for_someone_who_also_belongs_to_another_organization(client):
    owner, org = await create_user_with_org(client)
    shared = await add_member(org["id"], "staff")
    other_org = await create_org(client, await create_user(), "Elsewhere")
    await add_member(other_org["id"], "staff", user=shared)
    response = await client.post(reset_url(org, shared.id), headers=owner.headers)
    assert response.status_code == 409 and error(response)["code"] == "ACCOUNT_SHARED"


async def test_a_reset_sends_the_person_a_notice_without_the_password(client, worker, mailbox):
    from tests.conftest import run_jobs

    owner, org = await create_user_with_org(client)
    created = await create_person(client, owner, org, email="notice@example.com")
    response = await client.post(
        reset_url(org, created["member"]["user_id"]), headers=owner.headers
    )
    await run_jobs(worker)
    # The setup email from creating the person, then the notice of the reset.
    messages = mailbox.to("notice@example.com")
    assert "Your password was changed" in [m["Subject"] for m in messages]
    password = response.json()["data"]["temporary_password"]
    assert all(password not in mailbox.text(m) for m in messages)


async def test_the_person_gets_an_email_with_a_link_to_choose_a_password_not_the_password(
    client, worker, mailbox, db
):
    from tests.conftest import run_jobs

    owner, org = await create_user_with_org(client)
    mailbox.clear()
    response = await client.post(
        people_url(org),
        headers=owner.headers,
        json={
            "full_name": "Grace Hopper",
            "email": "grace@example.com",
            "role_id": await role_id(org["id"], "manager"),
        },
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["email_sent"] is True

    await run_jobs(worker)
    messages = mailbox.to("grace@example.com")
    assert [m["Subject"] for m in messages][-1].endswith("account is ready")
    body = mailbox.text(messages[-1])
    assert data["temporary_password"] not in body
    assert "/reset-password?token=" in body

    # The link lets the person choose a password, which ends the forced change.
    token = mailbox.token_for("grace@example.com")
    chosen = "a brand new passphrase 42"
    reset = await client.post(
        f"{API}/auth/reset-password",
        json={"token": token, "password": chosen, "password_confirm": chosen},
    )
    assert reset.status_code == 200
    actor = await sign_in(client, "grace@example.com", chosen)
    me = await client.get(f"{API}/users/me", headers=actor.headers)
    assert me.json()["data"]["must_change_password"] is False


async def test_no_email_is_queued_when_send_email_is_false(client, worker, mailbox):
    from tests.conftest import run_jobs

    owner, org = await create_user_with_org(client)
    mailbox.clear()
    response = await client.post(
        people_url(org),
        headers=owner.headers,
        json={
            "full_name": "Grace Hopper",
            "email": "grace@example.com",
            "role_id": await role_id(org["id"], "manager"),
            "send_email": False,
        },
    )
    assert response.status_code == 201
    assert response.json()["data"]["email_sent"] is False
    await run_jobs(worker)
    assert mailbox.to("grace@example.com") == []

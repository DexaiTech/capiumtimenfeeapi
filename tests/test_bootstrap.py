"""python -m app.maintenance.bootstrap"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.permissions import SYSTEM_ROLES
from app.maintenance.bootstrap import BootstrapError, bootstrap
from tests.conftest import ROOT
from tests.factories import API, create_user_with_org
from tests.foundation_helpers import sign_in

ADMIN = {"org_name": "Acme", "admin_name": "Ada Admin", "admin_email": "ada@acme.example"}


async def test_creates_the_organization_with_system_roles_and_a_verified_owner(db):
    result = await bootstrap(db, **ADMIN, slug="acme")
    assert result.organization.slug == "acme" and result.organization.name == "Acme"
    assert result.user.email_verified and result.user.must_change_password
    assert result.generated_password and len(result.generated_password) == 16

    roles = (
        await db.execute(
            text(
                "SELECT r.system_key, count(rp.permission_id) FROM roles r "
                "LEFT JOIN role_permissions rp ON rp.role_id = r.id "
                "WHERE r.organization_id = :org GROUP BY r.system_key"
            ),
            {"org": result.organization.id},
        )
    ).all()
    # Exactly what POST /organizations creates: every system role with its permissions.
    assert dict(roles) == {r.key: len(r.permissions) for r in SYSTEM_ROLES}
    member = (
        await db.execute(
            text(
                "SELECT r.system_key, m.status FROM organization_members m "
                "JOIN roles r ON r.id = m.role_id WHERE m.user_id = :u"
            ),
            {"u": result.user.id},
        )
    ).one()
    assert tuple(member) == ("owner", "active")
    assert await db.scalar(
        text("SELECT count(*) FROM audit_logs WHERE action = 'organization.created'")
    )
    # The practice gets the same defaults as one created through the API.
    org = {"org": result.organization.id}
    for table, expected in (
        ("vat_rates", 5),
        ("activity_codes", 11),
        ("services", 8),
        ("grades", 4),
        ("document_sequences", 7),
        ("ticket_tags", 7),
    ):
        count = await db.scalar(
            text(f"SELECT count(*) FROM {table} WHERE organization_id = :org"),  # noqa: S608
            org,
        )
        assert count == expected, table
    assert result.organization.currency == "GBP"
    assert result.organization.timezone == "Europe/London"


async def test_the_admin_signs_in_with_the_generated_password_and_must_change_it(client, db):
    result = await bootstrap(db, **ADMIN)
    admin = await sign_in(client, "ada@acme.example", result.generated_password or "")
    me = await client.get(f"{API}/users/me", headers=admin.headers)
    assert me.json()["data"]["must_change_password"] is True
    # Until the password is changed, the organization routes answer 403.
    blocked = await client.get(f"{API}/users/me/organizations", headers=admin.headers)
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"


async def test_a_given_password_is_used_and_not_generated(client, db):
    result = await bootstrap(db, **ADMIN, password="a very long passphrase")
    assert result.generated_password is None
    await sign_in(client, "ada@acme.example", "a very long passphrase")


async def test_refuses_when_an_organization_exists_and_changes_nothing(client, db):
    await create_user_with_org(client)
    users = await db.scalar(text("SELECT count(*) FROM users"))
    with pytest.raises(BootstrapError, match="already exists"):
        await bootstrap(db, **ADMIN)
    assert await db.scalar(text("SELECT count(*) FROM users")) == users
    assert await db.scalar(text("SELECT count(*) FROM organizations")) == 1


async def test_runs_only_once(db):
    await bootstrap(db, **ADMIN)
    with pytest.raises(BootstrapError):
        await bootstrap(db, **{**ADMIN, "admin_email": "other@acme.example"})
    assert await db.scalar(text("SELECT count(*) FROM organizations")) == 1


async def test_a_deleted_organization_does_not_block(db):
    first = await bootstrap(db, **ADMIN)
    await db.execute(
        text("UPDATE organizations SET deleted_at = now() WHERE id = :id"),
        {"id": first.organization.id},
    )
    await db.commit()
    again = await bootstrap(db, **{**ADMIN, "admin_email": "next@acme.example"})
    assert again.organization.id != first.organization.id


@pytest.mark.parametrize(
    "patch",
    [
        {"admin_email": "not-an-email"},
        {"org_name": ""},
        {"slug": "x"},
        {"slug": "Bad Slug"},
        {"admin_name": "  "},
        {"timezone": "Mars/Base"},
        {"password": "short"},
        {"password": "password123"},
    ],
)
async def test_invalid_input_is_refused_without_echoing_it(db, patch):
    with pytest.raises(BootstrapError) as raised:
        await bootstrap(db, **{**ADMIN, **patch})
    if "password" in patch:
        assert patch["password"] not in str(raised.value)
    assert await db.scalar(text("SELECT count(*) FROM organizations")) == 0
    assert await db.scalar(text("SELECT count(*) FROM users")) == 0


async def test_an_existing_email_is_refused(client, db):
    from tests.factories import create_user

    existing = await create_user(email="ada@acme.example")
    with pytest.raises(BootstrapError, match="email"):
        await bootstrap(db, **ADMIN)
    assert existing is not None


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "app.maintenance.bootstrap", *args],
        cwd=Path(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )


async def test_the_command_prints_only_the_generated_password(client, db):
    done = _run("--org-name", "Acme", "--admin-name", "Ada", "--admin-email", "ada@acme.example")
    assert done.returncode == 0, done.stderr
    password = next(
        line.split()[-1] for line in done.stdout.splitlines() if line.startswith("Password:")
    )
    assert len(password) == 16
    assert "Practice: Acme (acme)" in done.stdout and "Owner:" in done.stdout
    # The database URL, the JWT secret and the password hash never reach the output.
    secrets = [os.environ["DB_URL"], os.environ["JWT_SECRET_KEY"], "$argon2"]
    assert not any(s in done.stdout + done.stderr for s in secrets)
    await sign_in(client, "ada@acme.example", password)

    again = _run("--org-name", "Acme", "--admin-name", "Ada", "--admin-email", "b@acme.example")
    assert again.returncode == 1
    assert "already exists" in again.stderr
    assert password not in again.stdout + again.stderr


async def test_the_command_does_not_print_a_given_password(db):
    done = _run(
        "--org-name",
        "Acme",
        "--admin-name",
        "Ada",
        "--admin-email",
        "ada@acme.example",
        "--password",
        "my own long passphrase",
    )
    assert done.returncode == 0, done.stderr
    assert "my own long passphrase" not in done.stdout + done.stderr
    assert "Password:" not in done.stdout


async def test_the_command_reports_bad_input_with_exit_code_1():
    done = _run("--org-name", "Acme", "--admin-name", "Ada", "--admin-email", "nope")
    assert done.returncode == 1 and "Invalid input" in done.stderr
    blank = _run("--practice-name", " ", "--admin-name", "Ada", "--admin-email", "a@b.example")
    assert blank.returncode == 1 and "Invalid input: practice-name." in blank.stderr


async def test_the_command_takes_practice_name_and_says_practice_in_its_help(db):
    done = _run(
        "--practice-name", "Smith & Co", "--admin-name", "Ada", "--admin-email", "ada@acme.example"
    )
    assert done.returncode == 0, done.stderr
    assert await db.scalar(text("SELECT name FROM organizations")) == "Smith & Co"
    helped = _run("--help")
    assert helped.returncode == 0
    assert "--practice-name" in helped.stdout and "practice" in helped.stdout.lower()
    assert "organization" not in helped.stdout.lower()
    assert "Super admin" not in helped.stdout


async def test_refuses_with_a_clear_message_when_the_migrations_are_behind(db, monkeypatch):
    monkeypatch.setattr("app.maintenance.bootstrap.expected_revision", lambda: "999")
    with pytest.raises(BootstrapError, match="alembic upgrade head"):
        await bootstrap(db, org_name="Acme", admin_name="Ada", admin_email="ada@example.com")
    assert await db.scalar(text("SELECT count(*) FROM organizations")) == 0

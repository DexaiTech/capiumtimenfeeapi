"""Create the first practice and its Owner on an empty deployment.

A closed deployment has no sign-up, so the first account cannot come from the API. This
command makes the practice the normal way (the same service as ``POST /organizations``, so
the system roles, their permissions and the practice defaults - VAT rates, activity codes,
services and number sequences - are exactly the ones the API creates), a verified user, and
the Owner membership.

    python -m app.maintenance.bootstrap --practice-name "Acme Accountants" \\
        --admin-name "Ada" --admin-email ada@acme.co.uk [--slug acme] [--password ...]

``--org-name`` is accepted as an alias of ``--practice-name``. Without ``--password`` a
16-character password is generated and printed once. The Owner must change it at first
sign-in. The command refuses (exit code 1) when a practice already exists. It prints nothing
secret except that one generated password: never the database URL, never a password given
with ``--password``.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.health import expected_revision
from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.exceptions import AppException
from app.core.security import COMMON_PASSWORDS, generate_password, hash_password
from app.db.models import Organization, User
from app.db.session import unit_of_work
from app.repositories.organizations import OrganizationRepository
from app.repositories.users import UserRepository
from app.schemas.auth import PASSWORD_MAX, PASSWORD_MIN
from app.schemas.organization import OrganizationCreate
from app.services.organizations import OrganizationService
from app.utils.ids import new_id

_EMAIL: TypeAdapter[str] = TypeAdapter(EmailStr)
_LOCK_KEY = 20_261_011
# Practice fields as the operator typed them on the command line.
_OPTION_NAMES = {"name": "practice-name"}


class BootstrapError(Exception):
    """Something the operator can fix; the message is safe to print."""


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    organization: Organization
    user: User
    generated_password: str | None


def _validate_password(password: str) -> None:
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise BootstrapError(f"The password must be {PASSWORD_MIN} to {PASSWORD_MAX} characters.")
    if password.lower() in COMMON_PASSWORDS:
        raise BootstrapError("This password is too common. Choose another.")


async def bootstrap(
    session: AsyncSession,
    *,
    org_name: str,
    admin_name: str,
    admin_email: str,
    slug: str | None = None,
    password: str | None = None,
    timezone: str | None = None,
) -> BootstrapResult:
    """Create the practice and its Owner in one transaction.

    ``BootstrapError`` when a practice already exists, the address is taken or an input
    is invalid. ``generated_password`` is set only when no password was given.
    """
    admin_name = admin_name.strip()
    if not 1 <= len(admin_name) <= 120:
        raise BootstrapError("The admin name must be 1 to 120 characters.")
    try:
        email = _EMAIL.validate_python(admin_email.strip())
    except ValidationError as exc:
        raise BootstrapError("Invalid input: admin-email.") from exc
    try:
        data = OrganizationCreate(name=org_name, slug=slug, timezone=timezone)
    except ValidationError as exc:
        # Only the field names: an input's value is never echoed.
        fields = ", ".join(
            sorted(
                {
                    _OPTION_NAMES.get(str(e["loc"][-1]), str(e["loc"][-1]))
                    for e in exc.errors()
                    if e["loc"]
                }
            )
        )
        raise BootstrapError(f"Invalid input: {fields or 'practice'}.") from exc
    await _ensure_schema_is_current(session)
    generated = None if password else generate_password()
    chosen = password or generated or ""
    if password:
        _validate_password(password)
    password_hash = await hash_password(chosen)

    async with unit_of_work(session):
        # Two operators running this at once must not each create a practice.
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
        if await OrganizationRepository(session).first() is not None:
            raise BootstrapError("A practice already exists. Nothing was changed.")
        users = UserRepository(session)
        if await users.get_by_email(email) is not None:
            raise BootstrapError("A user with this email address already exists.")
        user = await users.add(
            User(
                id=new_id(),
                email=email,
                full_name=admin_name,
                password_hash=password_hash,
                email_verified_at=datetime.now(UTC),
                must_change_password=True,
            )
        )
        organization, _ = await OrganizationService(session).create(user, data, RequestContext())
    return BootstrapResult(organization, user, generated)


async def _ensure_schema_is_current(session: AsyncSession) -> None:
    """Stop with a clear message when the migrations have not been applied yet."""
    try:
        current = await session.scalar(text("SELECT version_num FROM alembic_version"))
    except ProgrammingError:
        await session.rollback()
        current = None
    expected = expected_revision()
    if current != expected:
        raise BootstrapError(
            f"The database schema is not up to date (found {current or 'none'}, "
            f"expected {expected}). Run `alembic upgrade head` first. Nothing was changed."
        )


async def _main(args: argparse.Namespace) -> int:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await bootstrap(
                session,
                org_name=args.org_name,
                slug=args.slug,
                admin_name=args.admin_name,
                admin_email=args.admin_email,
                password=args.password,
                timezone=args.timezone,
            )
    except (BootstrapError, AppException) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"Practice: {result.organization.name} ({result.organization.slug})")
    print(f"Owner:    {result.user.full_name} <{result.user.email}>")
    if result.generated_password:
        print(f"Password: {result.generated_password}")
        print("Shown once. The Owner must change it at first sign-in.")
    else:
        print("The Owner must change the password at first sign-in.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--practice-name",
        "--org-name",
        dest="org_name",
        metavar="NAME",
        required=True,
        help="practice name (1-120 characters)",
    )
    parser.add_argument(
        "--slug", help="URL slug (3-40 characters); derived from the name if left out"
    )
    parser.add_argument("--admin-name", required=True, help="full name of the Owner")
    parser.add_argument("--admin-email", required=True, help="email address of the Owner (login)")
    parser.add_argument(
        "--password", help="initial password (10-128 characters); generated and printed if left out"
    )
    parser.add_argument("--timezone", help="IANA time zone of the practice (default Europe/London)")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

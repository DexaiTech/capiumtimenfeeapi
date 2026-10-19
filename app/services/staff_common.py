"""Rules and errors shared by the staff services and the people and practice services that
create staff records."""

import uuid

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AppException, ConflictError
from app.core.security import generate_token, hash_password
from app.db.models import User
from app.services.common import duplicate_error
from app.services.members import INITIALS_INDEX, initials_taken_error, integrity_constraint
from app.utils.ids import new_id

STAFF_CODE_INDEX = "uq_organization_members_organization_id_staff_code"
STAFF_EMAIL_INDEX = "uq_organization_members_organization_id_work_email_active"
# Staff without a login have a placeholder account at this (non-routable) domain.
NO_LOGIN_DOMAIN = "no-login.invalid"
_unusable_hash: str | None = None


def staff_code_taken_error() -> ConflictError:
    return duplicate_error(
        "code", "Another staff member already has this code.", "STAFF_CODE_TAKEN"
    )


def staff_email_taken_error() -> ConflictError:
    return duplicate_error(
        "email",
        "Another active staff member already uses this email address.",
        "STAFF_EMAIL_TAKEN",
    )


def staff_integrity_error(exc: IntegrityError) -> AppException | None:
    """The error for a unique index of the staff record that a concurrent request won."""
    constraint = integrity_constraint(exc)
    if constraint == STAFF_CODE_INDEX:
        return staff_code_taken_error()
    if constraint == STAFF_EMAIL_INDEX:
        return staff_email_taken_error()
    if constraint == INITIALS_INDEX:
        return initials_taken_error()
    return None


def placeholder_email(user_id: uuid.UUID) -> str:
    return f"staff+{user_id}@{NO_LOGIN_DOMAIN}"


async def unusable_password_hash() -> str:
    """A password hash nobody knows the password of (for accounts that cannot sign in yet)."""
    global _unusable_hash
    if _unusable_hash is None:
        _unusable_hash = await hash_password(generate_token())
    return _unusable_hash


async def new_placeholder_user(full_name: str) -> User:
    """An inactive account that stands for a staff member without a login."""
    user_id = new_id()
    return User(
        id=user_id,
        email=placeholder_email(user_id),
        full_name=full_name,
        password_hash=await unusable_password_hash(),
        is_active=False,
        must_change_password=False,
    )

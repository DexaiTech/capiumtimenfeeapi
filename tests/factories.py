"""Test factories for the organization features.

Users and memberships are inserted directly, which is much faster than the register,
verify and invite flows and lets a test put anyone into any role and status.
"""

import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.tokens import create_access_token
from app.db.models import OrganizationMember, User, UserSession
from app.db.session import get_sessionmaker
from app.utils.ids import new_id

API = "/api/v1"
_counter = itertools.count(1)
_staff_numbers = itertools.count(1)


@dataclass(frozen=True)
class Actor:
    id: uuid.UUID
    email: str
    full_name: str
    headers: dict[str, str]


async def create_user(
    full_name: str | None = None, email: str | None = None, *, verified: bool = True
) -> Actor:
    """A user with a live session and a valid access token."""
    number = next(_counter)
    email = email or f"user{number}@example.com"
    full_name = full_name or f"User {number:03d}"
    now = datetime.now(UTC)
    user_id, session_id = new_id(), new_id()
    async with get_sessionmaker()() as session:
        session.add(
            User(
                id=user_id,
                email=email,
                full_name=full_name,
                password_hash="not-a-real-hash",
                email_verified_at=now if verified else None,
                password_changed_at=now - timedelta(minutes=5),
            )
        )
        await session.flush()
        session.add(UserSession(id=session_id, user_id=user_id, expires_at=now + timedelta(days=1)))
        await session.commit()
    token, _ = create_access_token(get_settings(), user_id=user_id, session_id=session_id)
    return Actor(user_id, email, full_name, {"Authorization": f"Bearer {token}"})


async def create_org(client: AsyncClient, owner: Actor, name: str = "Acme", **body: Any) -> dict:
    """Create an organization through the API. Returns the ``Organization`` object."""
    response = await client.post(
        f"{API}/organizations", headers=owner.headers, json={"name": name, **body}
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["organization"]


async def create_user_with_org(
    client: AsyncClient, name: str = "Acme", full_name: str | None = None, **body: Any
) -> tuple[Actor, dict]:
    owner = await create_user(full_name)
    return owner, await create_org(client, owner, name, **body)


async def role_id(organization_id: str | uuid.UUID, key_or_name: str) -> str:
    """The id of a role of the organization, by system key or by name."""
    async with get_sessionmaker()() as session:
        found = await session.scalar(
            text(
                "SELECT id FROM roles WHERE organization_id = :org AND deleted_at IS NULL "
                "AND (system_key = :key OR name = :key)"
            ),
            {"org": str(organization_id), "key": key_or_name},
        )
    assert found is not None, f"no role {key_or_name}"
    return str(found)


async def add_member(
    organization_id: str | uuid.UUID,
    role: str = "staff",
    *,
    user: Actor | None = None,
    full_name: str | None = None,
    email: str | None = None,
    status: str = "active",
) -> Actor:
    """Put a user (a new one unless given) into the organization with a role and status."""
    user = user or await create_user(full_name, email)
    async with get_sessionmaker()() as session:
        session.add(
            OrganizationMember(
                organization_id=uuid.UUID(str(organization_id)),
                user_id=user.id,
                role_id=uuid.UUID(await role_id(organization_id, role)),
                status=status,
                staff_code=f"T{next(_staff_numbers):05d}",
                work_email=user.email,
            )
        )
        await session.commit()
    return user


async def audit_entries(organization_id: str | uuid.UUID, action: str | None = None) -> list[dict]:
    """Audit rows of the organization, oldest first."""
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            text(
                "SELECT action, actor_user_id, target_type, target_id, metadata FROM audit_logs "
                "WHERE organization_id = :org AND (CAST(:action AS text) IS NULL "
                "OR action = :action) ORDER BY created_at, id"
            ),
            {"org": str(organization_id), "action": action},
        )
        return [
            {
                "action": row.action,
                "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
                "target_type": row.target_type,
                "target_id": str(row.target_id) if row.target_id else None,
                "metadata": row._mapping["metadata"],
            }
            for row in rows
        ]


def error(response: Any) -> dict:
    return response.json()["error"]

"""Helpers for the foundation tests: staff accounts made by an administrator, custom roles."""

import uuid
from collections.abc import Iterable
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text

from app.db.session import get_sessionmaker
from tests.factories import API, Actor, role_id

__all__ = ["API", "add_custom_role", "create_person", "people_url", "sign_in"]


def people_url(org: dict) -> str:
    return f"{API}/organizations/{org['id']}/people"


async def add_custom_role(
    organization_id: str | uuid.UUID, name: str, permissions: Iterable[str] = ()
) -> str:
    """Insert a custom role with exactly these permissions; returns its id.

    Every system role holds ``member.read`` and ``organization.read``, so tests that need a
    member *without* a permission build one with this.
    """
    new_role = str(uuid.uuid4())
    async with get_sessionmaker()() as session:
        await session.execute(
            text("INSERT INTO roles (id, organization_id, name) VALUES (:id, :org, :name)"),
            {"id": new_role, "org": str(organization_id), "name": name},
        )
        codes = sorted(set(permissions))
        if codes:
            await session.execute(
                text(
                    "INSERT INTO role_permissions (role_id, permission_id) "
                    "SELECT :role, id FROM permissions WHERE code = ANY(:codes)"
                ),
                {"role": new_role, "codes": codes},
            )
        await session.commit()
    return new_role


async def create_person(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    *,
    role: str = "staff",
    email: str = "newbie@example.com",
    full_name: str = "New Person",
    **body: Any,
) -> dict:
    """Create a person through the API; returns ``data`` (member and temporary password)."""
    response = await client.post(
        people_url(org),
        headers=actor.headers,
        json={
            "full_name": full_name,
            "email": email,
            "role_id": await role_id(org["id"], role),
            **body,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def sign_in(client: AsyncClient, email: str, password: str) -> Actor:
    """Log in with a password; returns an actor carrying the access token."""
    response = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    user = data["user"]
    return Actor(
        uuid.UUID(user["id"]),
        user["email"],
        user["full_name"],
        {"Authorization": f"Bearer {data['access_token']}"},
    )

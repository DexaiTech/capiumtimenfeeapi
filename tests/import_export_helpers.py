"""Helpers for the staff, rates and jobs CSV import and export tests."""

import csv
import io
from typing import Any

from httpx import AsyncClient, Response

from tests.clients_helpers import org_url
from tests.factories import Actor

__all__ = ["csv_of", "export_rows", "upload"]


def csv_of(*lines: str) -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode()


async def upload(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    kind: str,
    content: bytes,
    *,
    dry_run: bool = False,
    **query: Any,
) -> Response:
    """``POST /imports/{kind}`` with the CSV as a multipart ``file``."""
    params = {"dry_run": "true" if dry_run else None, **query}
    params = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items() if v}
    return await client.post(
        org_url(org, f"/imports/{kind}"),
        headers=actor.headers,
        params=params,
        files={"file": (f"{kind}.csv", content, "text/csv")},
    )


async def export_rows(
    client: AsyncClient, actor: Actor, org: dict, kind: str
) -> tuple[Response, list[dict[str, str]]]:
    """``GET /{kind}/export`` and its rows keyed by header (empty when it failed)."""
    response = await client.get(org_url(org, f"/{kind}/export"), headers=actor.headers)
    if response.status_code != 200:
        return response, []
    text = response.content.decode("utf-8-sig")
    return response, list(csv.DictReader(io.StringIO(text, newline="")))


async def role_with(org: dict, *codes: str) -> Actor:
    """A member of a custom role that holds exactly these permissions."""
    import uuid

    from sqlalchemy import select

    from app.db.models import Permission, Role, RolePermission
    from app.db.session import get_sessionmaker
    from tests.domain_factories import staff

    async with get_sessionmaker()() as session:
        role = Role(organization_id=uuid.UUID(org["id"]), name="Only " + ", ".join(codes))
        session.add(role)
        await session.flush()
        for code in codes:
            permission = await session.scalar(select(Permission).where(Permission.code == code))
            assert permission is not None, code
            session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        await session.commit()
        name = role.name
    return await staff(org, name)

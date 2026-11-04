"""Migration 079: the ``staff_only`` rate mode (APT79). A downgrade turns it back into
``staff_first`` (a person's own rate still comes first) and the older check refuses it again."""

import pytest
from sqlalchemy.exc import IntegrityError

from tests.clients_helpers import sql
from tests.domain_factories import practice
from tests.factories import API
from tests.migration_helpers import head_revision, migrate


async def test_downgrading_turns_staff_only_into_staff_first(client):
    owner, org = await practice(client)
    patched = await client.patch(
        f"{API}/organizations/{org['id']}", headers=owner.headers, json={"rate_mode": "staff_only"}
    )
    assert patched.status_code == 200, patched.text

    try:
        down = await migrate("downgrade", "077")
        assert down.returncode == 0, down.stderr
        assert await sql("SELECT rate_mode FROM organizations WHERE id = :o", o=org["id"]) == [
            {"rate_mode": "staff_first"}
        ]
        with pytest.raises(IntegrityError, match="ck_organizations_rate_mode"):
            await sql(
                "UPDATE organizations SET rate_mode = 'staff_only' WHERE id = :o", o=org["id"]
            )
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert await sql("SELECT version_num FROM alembic_version") == [
        {"version_num": head_revision()}
    ]

    patched = await client.patch(
        f"{API}/organizations/{org['id']}", headers=owner.headers, json={"rate_mode": "staff_only"}
    )
    assert patched.status_code == 200 and patched.json()["data"]["rate_mode"] == "staff_only"

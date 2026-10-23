"""POST /clients/bulk: set status, manager, partner and segment, and delete, on up to 200
clients, all or nothing, with the rules of the single operations."""

import uuid

from httpx import AsyncClient

from app.db.models import Client
from app.db.session import get_sessionmaker
from tests.domain_factories import make_client, make_job, practice, staff
from tests.factories import audit_entries
from tests.job_helpers import role_without
from tests.time_helpers import ok_data, org_url


async def _row(client_id) -> Client:
    async with get_sessionmaker()() as session:
        row = await session.get(Client, uuid.UUID(str(client_id)))
    assert row is not None
    return row


async def _bulk(client: AsyncClient, actor, org, ids, action, status=200, **extra):
    response = await client.post(
        org_url(org, "/clients/bulk"),
        headers=actor.headers,
        json={"client_ids": [str(i) for i in ids], "action": action, **extra},
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status == 200 else response.json()["error"]


async def _three(client: AsyncClient):
    owner, org = await practice(client)
    clients = [await make_client(org, f"Client {n} Ltd") for n in range(3)]
    return owner, org, clients, [c.id for c in clients]


async def test_set_status_and_archive(client: AsyncClient) -> None:
    owner, org, _, ids = await _three(client)
    result = await _bulk(client, owner, org, ids, "set_status", value="inactive")
    assert result == {"action": "set_status", "affected": 3, "changed": 3}
    assert {(await _row(i)).status for i in ids} == {"inactive"}
    again = await _bulk(client, owner, org, ids, "set_status", value="inactive")
    assert again["changed"] == 0
    await _bulk(client, owner, org, ids[:2], "set_status", value="archived")
    assert (await _row(ids[0])).status == "archived"
    assert (await _row(ids[2])).status == "inactive"
    archived = await audit_entries(org["id"], "client.archived")
    assert len(archived) == 2 and archived[0]["metadata"]["bulk"] is True
    assert archived[0]["metadata"]["changes"]["status"] == ["inactive", "archived"]
    await _bulk(client, owner, org, ids, "set_status", 422, value="gone")


async def test_archiving_needs_client_delete(client: AsyncClient) -> None:
    _owner, org, _, ids = await _three(client)
    manager = await staff(org, "manager")  # edits clients but may not archive or delete them
    await _bulk(client, manager, org, ids, "set_status", value="inactive")
    await _bulk(client, manager, org, ids, "set_status", 403, value="archived")
    await _bulk(client, manager, org, ids, "delete", 403)
    assert {(await _row(i)).status for i in ids} == {"inactive"}


async def test_set_and_clear_manager_and_partner(client: AsyncClient) -> None:
    owner, org, _, ids = await _three(client)
    person = await staff(org, "manager")
    result = await _bulk(client, owner, org, ids, "set_manager", value=str(person.id))
    assert result["changed"] == 3
    await _bulk(client, owner, org, ids, "set_partner", value=str(person.id))
    row = await _row(ids[1])
    assert row.manager_user_id == person.id and row.partner_user_id == person.id
    entry = (await audit_entries(org["id"], "client.updated"))[0]
    assert entry["metadata"]["bulk"] is True
    await _bulk(client, owner, org, ids, "set_manager")
    assert (await _row(ids[1])).manager_user_id is None
    assert (await _row(ids[1])).partner_user_id == person.id
    await _bulk(client, owner, org, ids, "set_partner", 422, value=str(uuid.uuid4()))
    await _bulk(client, owner, org, ids, "set_manager", 422, value="nope")


async def test_set_and_clear_segment(client: AsyncClient) -> None:
    owner, org, _, ids = await _three(client)
    await _bulk(client, owner, org, ids, "set_segment", value="Key account")
    assert {(await _row(i)).segment for i in ids} == {"Key account"}
    again = await _bulk(client, owner, org, ids, "set_segment", value="Key account")
    assert again["changed"] == 0
    await _bulk(client, owner, org, ids, "set_segment", value="")
    assert {(await _row(i)).segment for i in ids} == {None}
    await _bulk(client, owner, org, ids, "set_segment", 422, value="x" * 41)


async def test_unknown_and_foreign_ids_are_404(client: AsyncClient) -> None:
    owner, org, _, ids = await _three(client)
    _, _, _, others = await _three(client)
    error = await _bulk(client, owner, org, [*ids, others[0]], "set_status", 404, value="inactive")
    assert error["details"] == {"client_ids": [str(others[0])]}
    assert (await _row(ids[0])).status == "active"
    await _bulk(client, owner, org, [], "delete", 422)
    await _bulk(client, owner, org, ids, "delete", 422, value="x")


async def test_delete_clients_and_refuse_all_when_one_is_in_use(client: AsyncClient) -> None:
    owner, org, clients, ids = await _three(client)
    await make_job(org, clients[1])
    error = await _bulk(client, owner, org, ids, "delete", 409)
    assert error["code"] == "CLIENT_IN_USE"
    assert [d["client_id"] for d in error["details"]] == [str(ids[1])]
    assert error["details"][0]["usage"]["jobs"] == 1
    assert [(await _row(i)).deleted_at for i in ids] == [None] * 3
    free = [ids[0], ids[2]]
    result = await _bulk(client, owner, org, free, "delete")
    assert result == {"action": "delete", "affected": 2, "changed": 2}
    assert (await _row(ids[0])).deleted_at is not None
    rows = await audit_entries(org["id"], "client.deleted")
    assert len(rows) == 2 and rows[0]["metadata"]["bulk"] is True
    listed = ok_data(await client.get(org_url(org, "/clients"), headers=owner.headers))
    assert [c["id"] for c in listed] == [str(ids[1])]


async def test_permissions_are_those_of_the_single_operations(client: AsyncClient) -> None:
    _owner, org, _, ids = await _three(client)
    plain = await staff(org, "staff")
    nobody = await role_without(org, "client.read")
    await _bulk(client, plain, org, ids[:1], "set_segment", 403, value="A")
    await _bulk(client, plain, org, ids[:1], "delete", 403)
    await _bulk(client, nobody, org, ids[:1], "set_status", 403, value="inactive")
    assert (await _row(ids[0])).segment is None

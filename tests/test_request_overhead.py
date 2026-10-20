"""How many database statements a request costs before its handler has done any work.

Every statement is a network round trip, and on a remote database (~100+ ms away) the fixed cost of
authenticating and authorising a request is what makes the app feel slow. These tests pin it: a
signed-in request is one statement for the caller (user and session together), one for the
practice membership (organization, member, role and permissions together), and none for the
general rate limit (counted in memory)."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event

from app.core.config import get_settings
from app.db.session import get_engine
from tests.billing_helpers import new_practice, org_url


@contextmanager
def statements() -> Iterator[list[str]]:
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def test_a_signed_in_request_costs_one_statement_for_the_caller(client):
    p = await new_practice(client)
    with statements() as seen:
        response = await client.get("/api/v1/users/me", headers=p.owner.headers)
    assert response.status_code == 200, response.text
    # The user and its session together (the profile itself needs nothing more).
    assert len(seen) == 1, seen


async def test_an_organization_request_costs_two_statements_before_the_handler(client):
    p = await new_practice(client)
    with statements() as seen:
        response = await client.get(org_url(p.org, "/estimates/summary"), headers=p.owner.headers)
    assert response.status_code == 200, response.text
    # The caller (1), the practice membership with its role and permissions (1), and the one
    # aggregate of the handler itself.
    assert len(seen) == 3, seen


async def test_the_general_rate_limit_does_not_touch_the_database(client):
    settings = get_settings()
    settings.api_rate_limit_enabled = True
    try:
        with statements() as seen:
            response = await client.get("/api/v1/permissions")
        assert response.status_code == 401
        assert seen == []
        # The shared counter is still there for deployments that run several API processes.
        settings.api_rate_limit_store = "database"
        with statements() as seen:
            await client.get("/api/v1/permissions")
        assert len(seen) == 1 and "rate_limit_counters" in seen[0]
    finally:
        settings.api_rate_limit_enabled = False
        settings.api_rate_limit_store = "memory"


async def test_large_answers_are_compressed_and_small_ones_are_not(client):
    p = await new_practice(client)
    big = await client.get(
        "/openapi.json", headers={"accept-encoding": "gzip"}
    )  # a large JSON document
    assert big.status_code == 200
    assert big.headers.get("content-encoding") == "gzip"
    small = await client.get(org_url(p.org, "/estimates/summary"), headers={**p.owner.headers})
    assert small.status_code == 200
    assert "content-encoding" not in small.headers


async def test_warming_the_pool_opens_connections_and_survives_a_dead_database(monkeypatch):
    from app import main

    await main.warm_pool(2)  # works against the test database

    def broken():
        raise OSError("database unreachable")

    monkeypatch.setattr(main, "get_engine", broken)
    await main.warm_pool(2)  # only logged: the API still starts


async def test_file_downloads_are_not_compressed(client):
    from app.middleware.compression import JsonGZipMiddleware

    body = b"%PDF-" + b"0" * 5000
    sent: list[dict] = []

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/pdf"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"accept-encoding", b"gzip")],
    }
    await JsonGZipMiddleware(app)(scope, receive, send)
    headers = dict(sent[0]["headers"])
    assert b"content-encoding" not in headers
    assert headers[b"content-length"] == str(len(body)).encode()
    assert sent[1]["body"] == body

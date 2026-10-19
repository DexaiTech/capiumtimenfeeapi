import pytest
from httpx import AsyncClient

from app.core.logging import redact_processor
from app.main import app


@pytest.fixture(scope="module", autouse=True)
def _boom_route():
    async def boom() -> None:
        raise RuntimeError("secret internal detail /srv/app/db.py")

    app.add_api_route("/__boom", boom)
    yield
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/__boom"]


def assert_envelope(body: dict, code: str) -> None:
    assert body["success"] is False
    assert set(body["error"]) == {"code", "message", "details", "request_id"}
    assert body["error"]["code"] == code


async def test_unknown_route_uses_the_envelope(client: AsyncClient):
    response = await client.get("/api/v1/nope")
    assert response.status_code == 404
    assert_envelope(response.json(), "RESOURCE_NOT_FOUND")


async def test_wrong_method_uses_the_envelope(client: AsyncClient):
    response = await client.get("/api/v1/auth/login")
    assert response.status_code == 405
    assert_envelope(response.json(), "METHOD_NOT_ALLOWED")


async def test_malformed_json_is_a_validation_error(client: AsyncClient):
    response = await client.post(
        "/api/v1/auth/login", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert_envelope(response.json(), "VALIDATION_ERROR")


async def test_unexpected_exception_hides_details(client: AsyncClient):
    response = await client.get("/__boom")
    assert response.status_code == 500
    assert_envelope(response.json(), "INTERNAL_ERROR")
    assert "secret internal detail" not in response.text
    assert "Traceback" not in response.text


async def test_request_id_is_generated_returned_and_embedded(client: AsyncClient):
    response = await client.get("/api/v1/nope")
    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32
    assert response.json()["error"]["request_id"] == request_id


async def test_wellformed_inbound_request_id_is_kept_and_malformed_replaced(client: AsyncClient):
    kept = await client.get("/health/live", headers={"X-Request-ID": "trace-abc-12345"})
    assert kept.headers["x-request-id"] == "trace-abc-12345"
    replaced = await client.get("/health/live", headers={"X-Request-ID": "bad id\twith spaces"})
    assert replaced.headers["x-request-id"] != "bad id\twith spaces"


async def test_security_headers_are_set(client: AsyncClient):
    response = await client.get("/health/live")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]


async def test_cors_allows_only_the_configured_origin(client: AsyncClient):
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization",
    }
    allowed = await client.options(
        "/api/v1/auth/login", headers={"Origin": "http://localhost:5173", **preflight}
    )
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    denied = await client.options(
        "/api/v1/auth/login", headers={"Origin": "https://evil.example", **preflight}
    )
    assert "access-control-allow-origin" not in denied.headers


async def test_health_endpoints(client: AsyncClient):
    assert (await client.get("/health/live")).status_code == 200
    assert (await client.get("/health/ready")).status_code == 200


async def test_openapi_documents_routes_with_summaries(client: AsyncClient):
    schema = (await client.get("/openapi.json")).json()
    for path, operations in schema["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        for operation in operations.values():
            assert operation.get("summary"), path
            assert operation.get("tags"), path
    assert "/api/v1/auth/refresh" in schema["paths"]


def test_logs_redact_secrets():
    event = redact_processor(
        None,
        "info",
        {
            "event": "x",
            "password": "hunter2",
            "headers": {"Authorization": "Bearer abc", "accept": "json"},
            "refresh_token": "abc",
            "user_id": "42",
        },
    )
    assert event["password"] == "[redacted]"
    assert event["refresh_token"] == "[redacted]"
    assert event["headers"] == {"Authorization": "[redacted]", "accept": "json"}
    assert event["user_id"] == "42"

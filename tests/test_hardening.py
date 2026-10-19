"""The global rate limit, the request size cap and the extra security headers."""

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.main import app
from app.middleware.hardening import _RATE_EXEMPT, _UPLOADS


@pytest.fixture(scope="module", autouse=True)
def _echo_route():
    async def echo(payload: dict) -> dict:
        return {"keys": sorted(payload)}

    app.add_api_route("/api/v1/__echo", echo, methods=["POST"])
    yield
    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", "") != "/api/v1/__echo"
    ]


@pytest.fixture
def limits(monkeypatch):
    settings = get_settings()

    def apply(**values):
        for name, value in values.items():
            monkeypatch.setattr(settings, name, value)

    return apply


async def test_signed_out_requests_are_limited_per_address(client: AsyncClient, limits):
    limits(api_rate_limit_enabled=True, api_rate_limit_ip_per_minute=3)
    for _ in range(3):
        assert (await client.get("/api/v1/permissions")).status_code == 401  # reached the route
    limited = await client.get("/api/v1/permissions")
    assert limited.status_code == 429
    error = limited.json()["error"]
    assert error["code"] == "RATE_LIMITED" and error["request_id"]
    assert 1 <= int(limited.headers["retry-after"]) <= 60
    assert limited.headers["x-content-type-options"] == "nosniff"  # outer middleware still ran


async def test_signed_in_requests_are_limited_per_member_not_per_address(
    client: AsyncClient, verified_user, limits
):
    limits(
        api_rate_limit_enabled=True,
        api_rate_limit_user_per_minute=4,
        api_rate_limit_ip_per_minute=2,
    )
    headers = verified_user["headers"]
    for _ in range(4):
        assert (await client.get("/api/v1/users/me", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/users/me", headers=headers)).status_code == 429
    # The address limit applies to people who are not signed in, not to this member.
    other = await client.get("/api/v1/users/me", headers={"Authorization": "Bearer not.a.token"})
    assert other.status_code in (401, 429)


async def test_health_options_and_streams_are_not_counted(client: AsyncClient, limits):
    limits(api_rate_limit_enabled=True, api_rate_limit_ip_per_minute=1)
    for _ in range(5):
        assert (await client.get("/health/live")).status_code == 200
        assert (
            await client.options(
                "/api/v1/auth/login",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "POST",
                },
            )
        ).status_code == 200
    for path in ("/health/ready", "/docs", "/redoc", "/openapi.json"):
        assert _RATE_EXEMPT.match(path), path
    for path in ("/api/v1/permissions", "/api/v1/organizations/abc/reports/wip", "/healthz"):
        assert not _RATE_EXEMPT.match(path), path


async def test_limiter_can_be_turned_off(client: AsyncClient, limits):
    limits(api_rate_limit_enabled=False, api_rate_limit_ip_per_minute=1)
    for _ in range(4):
        assert (await client.get("/api/v1/permissions")).status_code == 401


async def test_oversized_bodies_are_refused_by_declared_length(client: AsyncClient, limits):
    limits(max_request_body_kb=1)
    ok = await client.post("/api/v1/__echo", json={"a": "x" * 100})
    assert ok.status_code == 200 and ok.json() == {"keys": ["a"]}
    big = await client.post("/api/v1/__echo", json={"a": "x" * 5000})
    assert big.status_code == 413
    error = big.json()["error"]
    assert error["code"] == "REQUEST_TOO_LARGE" and error["details"] == {"max_bytes": 1024}


async def test_oversized_chunked_bodies_are_refused_without_a_declared_length(
    client: AsyncClient, limits
):
    limits(max_request_body_kb=1)

    async def chunks():
        yield b'{"a": "'
        for _ in range(20):
            yield b"x" * 500
        yield b'"}'

    response = await client.post(
        "/api/v1/__echo", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


async def test_reads_and_upload_endpoints_are_not_capped(
    client: AsyncClient, verified_user, limits
):
    limits(max_request_body_kb=1)
    assert (
        await client.get("/api/v1/users/me", headers=verified_user["headers"])
    ).status_code == 200
    # Only bulk imports carry big bodies; they enforce their own limit.
    assert _UPLOADS.match(
        "/api/v1/organizations/0198c1f2-6c7e-7d3a-9b1e-2f6f0a4d5e11/imports/clients"
    )
    for path in (
        "/api/v1/users/me/avatar",
        "/api/v1/organizations/x/imports",
        "/api/v1/organizations/x/imports/clients/extra",
        "/api/v1/organizations/x/invoices",
        "/api/v1/organizations/x/clients",
    ):
        assert not _UPLOADS.match(path), path
    # A capped write route still answers 413 for a large body.
    big = await client.patch(
        "/api/v1/users/me", headers=verified_user["headers"], json={"full_name": "x" * 5000}
    )
    assert big.status_code == 413


async def test_extra_security_headers(client: AsyncClient):
    response = await client.get("/health/live")
    assert response.headers["cross-origin-resource-policy"] == "same-site"
    assert response.headers["permissions-policy"] == "interest-cohort=()"

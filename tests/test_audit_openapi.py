"""The audit-log routes are documented in the OpenAPI schema: summaries, descriptions that name the
permissions, the error responses, and a description on every parameter and field the UI builds on."""

from app.services.xlsx_export import XLSX_MEDIA_TYPE

PREFIX = "/api/v1/organizations/{organization_id}/audit-logs"
FILTERS = {"action", "actor_user_id", "from", "to", "q"}
PAGING = {"limit", "cursor"}
RECORD = {"target_type", "target_id"}

# (path suffix, the query parameters, the permission codes the description names)
ROUTES = [
    ("", PAGING | FILTERS | RECORD, ["audit_log.read"]),
    (
        "/history",
        PAGING | FILTERS | RECORD | {"include_related"},
        ["audit_log.read", "client.read", "invoice.read"],
    ),
    ("/export", FILTERS | RECORD | {"format"}, ["audit_log.read", "audit_log.export"]),
]


async def load(client) -> dict:
    return (await client.get("/openapi.json")).json()


def queries(operation: dict) -> dict[str, dict]:
    return {p["name"]: p for p in operation.get("parameters", []) if p["in"] == "query"}


async def test_the_routes_are_documented(client):
    spec = await load(client)
    for suffix, _, permissions in ROUTES:
        item = spec["paths"][PREFIX + suffix]
        # Nothing but reading: no method changes the trail.
        assert set(item) == {"get"}, suffix
        operation = item["get"]
        assert operation["summary"], suffix
        assert operation["tags"] == ["Audit"], suffix
        assert len(operation["description"]) > 60, suffix
        for code in permissions:
            assert f"`{code}`" in operation["description"], (suffix, code)
        assert {"200", "401", "403", "404", "422"} <= set(operation["responses"]), suffix


async def test_every_parameter_is_described_and_the_sets_are_the_documented_ones(client):
    spec = await load(client)
    for suffix, expected, _ in ROUTES:
        operation = spec["paths"][PREFIX + suffix]["get"]
        found = queries(operation)
        assert set(found) == expected, suffix
        for name, parameter in found.items():
            assert parameter.get("description"), (suffix, name)
        for parameter in operation["parameters"]:
            if parameter["in"] == "path":
                assert parameter["name"] == "organization_id"


async def test_the_history_needs_its_record_and_the_list_does_not(client):
    spec = await load(client)
    history = queries(spec["paths"][PREFIX + "/history"]["get"])
    assert history["target_type"]["required"] is True
    assert history["target_id"]["required"] is True
    assert history["include_related"]["schema"]["default"] is False
    listed = queries(spec["paths"][PREFIX]["get"])
    assert not listed["target_type"].get("required") and not listed["target_id"].get("required")
    # The same bounds everywhere a page is asked for.
    for found in (history, listed):
        assert found["limit"]["schema"]["minimum"] == 1
        assert found["limit"]["schema"]["maximum"] == 100
    pattern = history["target_type"]["schema"]["pattern"]
    assert pattern == "^[a-z][a-z0-9_]*$"


async def test_the_export_documents_its_formats_and_its_file_responses(client):
    spec = await load(client)
    operation = spec["paths"][PREFIX + "/export"]["get"]
    fmt = queries(operation)["format"]["schema"]
    assert fmt["enum"] == ["csv", "xlsx"] and fmt["default"] == "csv"
    ok = operation["responses"]["200"]
    assert set(ok["content"]) == {"text/csv", XLSX_MEDIA_TYPE}
    assert "X-Export-Truncated" in ok["description"]
    for text in ("50,000", "audit_log.exported", "BOM"):
        assert text in operation["description"], text


async def test_every_field_of_the_response_models_is_described(client):
    models = (await load(client))["components"]["schemas"]
    expected = {
        "AuditLogEntryRead": {
            "id",
            "created_at",
            "action",
            "label",
            "summary",
            "actor",
            "target_type",
            "target_id",
            "ip_address",
            "user_agent",
            "changes",
            "metadata",
        },
        "AuditHistoryEntryRead": {
            "id",
            "action",
            "label",
            "actor",
            "at",
            "ip_address",
            "target_type",
            "target_id",
            "changes",
            "summary",
        },
        "AuditChange": {"field", "before", "after"},
        "AuditActor": {"id", "full_name", "email"},
        "AuditHistoryActor": {"id", "name"},
    }
    missing = []
    for name, fields in expected.items():
        assert set(models[name]["properties"]) == fields, name
        for field, prop in models[name]["properties"].items():
            if not prop.get("description"):
                missing.append(f"{name}.{field}")
    assert not missing, missing

"""The staff, rates and jobs import and export routes are documented in the OpenAPI schema."""

PREFIX = "/api/v1/organizations/{organization_id}"
EXPECTED = (
    ("get", "/staff/export"),
    ("post", "/imports/staff"),
    ("get", "/rates/export"),
    ("post", "/imports/rates"),
    ("get", "/jobs/export"),
    ("post", "/imports/jobs"),
)


async def test_routes_are_documented(client):
    schema = (await client.get("/openapi.json")).json()
    for method, path in EXPECTED:
        operation = schema["paths"][PREFIX + path][method]
        assert operation["summary"], path
        assert len(operation["description"]) > 100, path
        assert "data.import_export" in operation["description"], path
        assert "Import & export" in operation["tags"], path
        assert {"401", "403", "404", "422"} <= set(operation["responses"]), path
        if method == "get":
            assert "text/csv" in operation["responses"]["200"]["content"], path
        else:
            assert {"400", "409", "413", "415"} <= set(operation["responses"]), path
            content = operation["requestBody"]["content"]
            assert set(content) == {"multipart/form-data", "text/csv"}, path
            queries = [p for p in operation["parameters"] if p["in"] == "query"]
            assert "dry_run" in {p["name"] for p in queries}, path
            assert all(p.get("description") for p in queries), path


async def test_rates_import_documents_close_previous(client):
    schema = (await client.get("/openapi.json")).json()
    parameters = schema["paths"][PREFIX + "/imports/rates"]["post"]["parameters"]
    assert "close_previous" in {p["name"] for p in parameters}


async def test_import_result_schema_is_published(client):
    schema = (await client.get("/openapi.json")).json()
    result = schema["components"]["schemas"]["BulkImportResult"]
    assert {"dry_run", "total_rows", "valid_rows", "created", "errors", "items", "counts"} <= set(
        result["properties"]
    )

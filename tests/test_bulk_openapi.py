"""The bulk routes of jobs, clients and invoices are documented in the OpenAPI schema, declared
before the routes with an id, and their bodies say what each action takes."""

PREFIX = "/api/v1/organizations/{organization_id}"
EXPECTED = {
    "/jobs/bulk": ("Jobs", "job_ids", ["set_status", "set_manager", "set_due_date", "delete"]),
    "/clients/bulk": (
        "Clients",
        "client_ids",
        ["set_status", "set_manager", "set_partner", "set_segment", "delete"],
    ),
    "/invoices/bulk": ("Invoices", "invoice_ids", ["delete"]),
}


async def test_bulk_routes_are_documented(client):
    schema = (await client.get("/openapi.json")).json()
    for path, (tag, ids, actions) in EXPECTED.items():
        operation = schema["paths"][PREFIX + path]["post"]
        assert operation["summary"].startswith("Bulk action"), path
        assert len(operation["description"]) > 200, path
        assert "all or nothing" in operation["description"], path
        assert tag in operation["tags"], path
        assert {"401", "403", "404", "409", "422"} <= set(operation["responses"]), path
        ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        body = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]
        assert body["properties"][ids]["minItems"] == 1, path
        assert body["properties"][ids]["maxItems"] == 200, path
        assert body["properties"]["action"].get("enum", actions) == actions, path
        assert body["additionalProperties"] is False, path
        result = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert "BulkChangeResult" in str(result), path


async def test_bulk_is_declared_before_the_id_routes(client):
    paths = list((await client.get("/openapi.json")).json()["paths"])
    for resource, bulk in (
        ("jobs", "job_id"),
        ("clients", "client_id"),
        ("invoices", "invoice_id"),
    ):
        assert paths.index(f"{PREFIX}/{resource}/bulk") < paths.index(
            f"{PREFIX}/{resource}/{{{bulk}}}"
        )

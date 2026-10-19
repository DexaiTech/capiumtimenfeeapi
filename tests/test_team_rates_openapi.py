"""The team, rates and activity-link routes are documented in the OpenAPI schema."""

PREFIX = "/api/v1/organizations/{organization_id}"
ANY_MEMBER_READS = {
    ("get", "/services/{service_id}/activity-codes"),
    ("get", "/activity-codes/{activity_id}"),
}
EXPECTED = {
    ("get", "/staff"): "Staff",
    ("post", "/staff"): "Staff",
    ("get", "/staff/{staff_id}"): "Staff",
    ("patch", "/staff/{staff_id}"): "Staff",
    ("delete", "/staff/{staff_id}"): "Staff",
    ("post", "/staff/{staff_id}/deactivate"): "Staff",
    ("post", "/staff/{staff_id}/reactivate"): "Staff",
    ("post", "/staff/{staff_id}/invite"): "Staff",
    ("post", "/staff/{staff_id}/link-login"): "Staff",
    ("post", "/staff/{staff_id}/unlink-login"): "Staff",
    ("get", "/grades"): "Grades",
    ("post", "/grades"): "Grades",
    ("get", "/grades/{grade_id}"): "Grades",
    ("patch", "/grades/{grade_id}"): "Grades",
    ("delete", "/grades/{grade_id}"): "Grades",
    ("post", "/grades/{grade_id}/activate"): "Grades",
    ("post", "/grades/{grade_id}/deactivate"): "Grades",
    ("get", "/rates"): "Rates",
    ("post", "/rates"): "Rates",
    ("get", "/rates/resolve"): "Rates",
    ("get", "/rates/history"): "Rates",
    ("post", "/rates/rerate/preview"): "Rates",
    ("post", "/rates/rerate"): "Rates",
    ("get", "/rates/reruns"): "Rates",
    ("get", "/rates/reruns/{run_id}"): "Rates",
    ("get", "/rates/reruns/{run_id}/items"): "Rates",
    ("post", "/rates/revalue"): "Rates",
    ("get", "/rates/{rule_id}"): "Rates",
    ("patch", "/rates/{rule_id}"): "Rates",
    ("delete", "/rates/{rule_id}"): "Rates",
    ("get", "/activity-codes/{activity_id}"): "Settings",
    ("patch", "/activity-codes/{activity_id}"): "Settings",
    ("post", "/activity-codes/{activity_id}/activate"): "Settings",
    ("post", "/activity-codes/{activity_id}/deactivate"): "Settings",
    ("get", "/services/{service_id}/activity-codes"): "Settings",
    ("put", "/services/{service_id}/activity-codes"): "Settings",
}
CONFLICTS = {
    ("post", "/staff"),
    ("patch", "/staff/{staff_id}"),
    ("delete", "/staff/{staff_id}"),
    ("post", "/staff/{staff_id}/deactivate"),
    ("post", "/staff/{staff_id}/reactivate"),
    ("post", "/staff/{staff_id}/invite"),
    ("post", "/staff/{staff_id}/link-login"),
    ("post", "/staff/{staff_id}/unlink-login"),
    ("post", "/grades"),
    ("patch", "/grades/{grade_id}"),
    ("post", "/rates"),
    ("patch", "/rates/{rule_id}"),
    ("delete", "/rates/{rule_id}"),
    ("post", "/rates/rerate"),
    ("patch", "/activity-codes/{activity_id}"),
}


async def test_routes_are_documented(client):
    schema = (await client.get("/openapi.json")).json()
    for (method, path), tag in EXPECTED.items():
        operation = schema["paths"][PREFIX + path][method]
        assert operation["summary"], (method, path)
        assert len(operation["description"]) > 20, (method, path)
        assert tag in operation["tags"], (method, path)
        assert {"401", "404", "422"} <= set(operation["responses"]), (method, path)
        if (method, path) not in ANY_MEMBER_READS:
            assert "403" in operation["responses"], (method, path)
        if (method, path) in CONFLICTS:
            assert "409" in operation["responses"], (method, path)


LISTS = (
    ("get", "/staff"),
    ("get", "/grades"),
    ("get", "/rates"),
    ("get", "/rates/resolve"),
    ("get", "/rates/history"),
    ("get", "/rates/reruns"),
    ("get", "/rates/reruns/{run_id}/items"),
    ("get", "/services"),
    ("get", "/activity-codes"),
)


async def test_query_parameters_are_described(client):
    schema = (await client.get("/openapi.json")).json()
    for method, path in LISTS:
        operation = schema["paths"][PREFIX + path][method]
        queries = [p for p in operation["parameters"] if p["in"] == "query"]
        assert queries, path
        for parameter in queries:
            assert parameter.get("description"), (path, parameter["name"])


async def test_every_new_operation_has_a_unique_id(client):
    schema = (await client.get("/openapi.json")).json()
    ids = [
        operation["operationId"]
        for methods in schema["paths"].values()
        for operation in methods.values()
    ]
    assert len(ids) == len(set(ids))

"""The dashboard pages are documented in the OpenAPI schema."""

PREFIX = "/api/v1/organizations/{organization_id}/dashboard"
ROUTES = (
    "/cards",
    "/my-actions",
    "/my-timesheet",
    "/pending-approvals",
    "/missing-timesheets",
    "/top-clients-by-wip",
    "/billing-trend",
    "/debtor-ageing",
)
MODELS = (
    "DashboardCards",
    "DashboardCard",
    "CardComparison",
    "CardFigure",
    "MyActions",
    "MyAction",
    "MyTimesheet",
    "PendingApprovals",
    "ApprovalItem",
    "MissingTimesheetsWidget",
    "TopClientsByWip",
    "WipClient",
    "BillingTrend",
    "BillingMonth",
    "DebtorAgeing",
    "AgeingBucket",
    "DebtorRow",
)


async def test_every_route_is_documented(client):
    schema = (await client.get("/openapi.json")).json()
    for path in ROUTES:
        operation = schema["paths"][PREFIX + path]["get"]
        assert operation["summary"], path
        assert len(operation["description"]) > 150, path
        assert "Dashboard" in operation["tags"], path
        declared = set(operation["responses"])
        # A widget the caller may not see answers 200 with `visible: false`: there is no 403.
        assert {"200", "401", "404", "422"} <= declared, path
        assert "403" not in declared, path


async def test_query_parameters_are_described_and_bounded(client):
    schema = (await client.get("/openapi.json")).json()
    expected = {
        "/cards": {"period"},
        "/my-actions": {"limit"},
        "/my-timesheet": set(),
        "/pending-approvals": {"kind", "limit"},
        "/missing-timesheets": {"week_start"},
        "/top-clients-by-wip": {"limit", "period"},
        "/billing-trend": {"months"},
        "/debtor-ageing": {"limit", "period"},
    }
    for path, names in expected.items():
        operation = schema["paths"][PREFIX + path]["get"]
        queries = {p["name"]: p for p in operation.get("parameters", []) if p["in"] == "query"}
        assert set(queries) == names, path
        for name, parameter in queries.items():
            assert parameter.get("description"), (path, name)
    period = schema["paths"][PREFIX + "/cards"]["get"]["parameters"]
    values = next(p for p in period if p["name"] == "period")["schema"]
    assert values["enum"] == ["this_week", "this_month", "ytd"]
    months = next(
        p
        for p in schema["paths"][PREFIX + "/billing-trend"]["get"]["parameters"]
        if p["name"] == "months"
    )["schema"]
    assert (months["minimum"], months["maximum"], months["default"]) == (1, 24, 12)


async def test_the_response_models_are_described(client):
    schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]
    for name in MODELS:
        assert name in schemas, name
    widget = schemas["MyTimesheet"]["properties"]
    assert {"visible", "as_at", "generated_at", "days", "entries", "timer"} <= set(widget)
    assert widget["visible"]["description"]
    card = schemas["DashboardCard"]["properties"]
    assert {"key", "title", "visible", "unit", "value", "comparison", "secondary", "link"} == set(
        card
    )
    assert set(schemas["CardComparison"]["properties"]) == {
        "basis",
        "previous",
        "change",
        "change_bp",
    }
    action = schemas["MyAction"]["properties"]
    assert {
        "id",
        "kind",
        "priority",
        "title",
        "subject",
        "context",
        "date",
        "count",
        "link",
    } <= set(action)
    assert schemas["MyAction"]["properties"]["priority"]["enum"] == ["high", "medium", "low"]
    assert set(schemas["AgeingBucket"]["properties"]["key"]["enum"]) == {
        "current",
        "days_1_30",
        "days_31_60",
        "days_61_90",
        "days_90_plus",
    }


async def test_the_existing_dashboard_is_unchanged(client):
    schema = (await client.get("/openapi.json")).json()
    paths = schema["paths"]
    assert "get" in paths["/api/v1/organizations/{organization_id}/dashboard"]
    properties = schema["components"]["schemas"]["Dashboard"]["properties"]
    assert set(properties) == {
        "today",
        "week",
        "timer",
        "unsubmitted_week_count",
        "unsubmitted_weeks",
        "jobs_due",
        "approvals_waiting",
        "financial",
    }
    ids = [op["operationId"] for methods in paths.values() for op in methods.values()]
    assert len(ids) == len(set(ids))

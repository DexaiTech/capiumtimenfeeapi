"""The routes added for time recording gaps are documented in the OpenAPI schema."""

PREFIX = "/api/v1/organizations/{organization_id}"
EXPECTED = {
    ("get", "/time-entries/day"): ("Time", {403, 404}),
    ("get", "/time-entries/calendar"): ("Time", {403, 404}),
    ("post", "/time-entries/copy-week"): ("Time", {403, 404, 409}),
    ("post", "/time-entries/{entry_id}/copy"): ("Time", {403, 404, 409}),
    ("post", "/time-entries/{entry_id}/correct"): ("Time", {403, 404, 409}),
    ("get", "/time-entries/{entry_id}/history"): ("Time", {403, 404}),
    ("get", "/timesheets/missing"): ("Timesheets", {403, 404}),
    ("post", "/timesheets/reminders/run"): ("Timesheets", {403}),
}
QUERIED = (
    ("get", "/time-entries/week"),
    ("get", "/time-entries/day"),
    ("get", "/time-entries/calendar"),
    ("get", "/time-entries/{entry_id}/history"),
    ("get", "/timesheets/missing"),
)


async def test_routes_are_documented(client):
    schema = (await client.get("/openapi.json")).json()
    for (method, path), (tag, errors) in EXPECTED.items():
        operation = schema["paths"][PREFIX + path][method]
        assert operation["summary"], (method, path)
        assert len(operation["description"]) > 60, (method, path)
        assert tag in operation["tags"], (method, path)
        declared = set(operation["responses"])
        assert {"401", "422"} | {str(code) for code in errors} <= declared, (method, path)


async def test_query_parameters_are_described(client):
    schema = (await client.get("/openapi.json")).json()
    for method, path in QUERIED:
        operation = schema["paths"][PREFIX + path][method]
        queries = [p for p in operation["parameters"] if p["in"] == "query"]
        assert queries, path
        for parameter in queries:
            if parameter["name"] in ("limit", "cursor"):
                continue
            assert parameter.get("description"), (path, parameter["name"])


async def test_the_documented_shapes_have_the_new_fields(client):
    schema = (await client.get("/openapi.json")).json()
    schemas = schema["components"]["schemas"]
    entry = schemas["TimeEntryRead"]["properties"]
    assert {"internal_note", "service_id", "service_name"} <= set(entry)
    week = (
        schemas["WeekGrid-Output"]["properties"]
        if "WeekGrid-Output" in schemas
        else schemas["WeekGrid"]["properties"]
    )
    assert {"targets", "working_days", "missing_days", "day_variance", "today"} <= set(week)
    for name in ("TimeEntryCreate", "TimeEntryUpdate"):
        body = schemas[name]["properties"]
        assert "internal_note" in body, name
    correction = schemas["TimeCorrection"]["properties"]
    assert {"reason", "minutes", "client_id", "job_id", "activity_code_id", "description"} <= set(
        correction
    )
    assert {"internal_note"} <= set(correction)
    for name in (
        "MissingTimesheet",
        "RemindersResult",
        "CopyWeekResult",
        "TimeDay",
        "TimeCalendar",
    ):
        assert name in schemas, name
    assert {"source_entry_id", "code", "message"} == set(schemas["CopySkipped"]["properties"])


async def test_every_operation_id_is_unique(client):
    schema = (await client.get("/openapi.json")).json()
    ids = [
        operation["operationId"]
        for methods in schema["paths"].values()
        for operation in methods.values()
    ]
    assert len(ids) == len(set(ids))

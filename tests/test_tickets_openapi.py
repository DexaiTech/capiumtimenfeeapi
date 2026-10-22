"""The Application Testing routes are documented in the OpenAPI schema, with the shapes the UI is
built from."""

from tests.ticket_helpers import DASHBOARD_KEYS, DETAIL_KEYS, ITEM_KEYS

STATUSES = [
    "open",
    "in_progress",
    "requirement_pending",
    "clarification_required",
    "blocked",
    "ready_for_ba_review",
    "reopened",
    "closed",
    "invalid",
    "on_hold",
]
PREFIX = "/api/v1/organizations/{organization_id}"
EXPECTED = {
    ("get", "/tickets"): "Tickets",
    ("post", "/tickets"): "Tickets",
    ("get", "/tickets/dashboard"): "Tickets",
    ("post", "/tickets/bulk"): "Tickets",
    ("get", "/tickets/{ticket_id}"): "Tickets",
    ("patch", "/tickets/{ticket_id}"): "Tickets",
    ("get", "/tickets/{ticket_id}/timeline"): "Tickets",
    ("get", "/tickets/{ticket_id}/comments"): "Tickets",
    ("post", "/tickets/{ticket_id}/comments"): "Tickets",
    ("patch", "/tickets/{ticket_id}/comments/{comment_id}"): "Tickets",
    ("delete", "/tickets/{ticket_id}/comments/{comment_id}"): "Tickets",
    ("get", "/tickets/{ticket_id}/attachments"): "Tickets",
    ("post", "/tickets/{ticket_id}/attachments"): "Tickets",
    ("get", "/tickets/{ticket_id}/attachments/{attachment_id}"): "Tickets",
    ("delete", "/tickets/{ticket_id}/attachments/{attachment_id}"): "Tickets",
    ("get", "/ticket-tags"): "Ticket tags",
    ("post", "/ticket-tags"): "Ticket tags",
    ("patch", "/ticket-tags/{tag_id}"): "Ticket tags",
    ("delete", "/ticket-tags/{tag_id}"): "Ticket tags",
}
CONFLICTS = {
    ("post", "/tickets/{ticket_id}/attachments"),
    ("delete", "/tickets/{ticket_id}/attachments/{attachment_id}"),
    ("post", "/ticket-tags"),
    ("patch", "/ticket-tags/{tag_id}"),
    ("delete", "/ticket-tags/{tag_id}"),
}


async def test_routes_are_documented(client):
    schema = (await client.get("/openapi.json")).json()
    for (method, path), tag in EXPECTED.items():
        operation = schema["paths"][PREFIX + path][method]
        assert operation["summary"], (method, path)
        assert len(operation["description"]) > 40, (method, path)
        assert tag in operation["tags"], (method, path)
        assert {"401", "403", "404", "422"} <= set(operation["responses"]), (method, path)
        assert ("409" in operation["responses"]) == ((method, path) in CONFLICTS), (method, path)


async def test_query_parameters_are_described(client):
    schema = (await client.get("/openapi.json")).json()
    queried = {
        ("get", "/tickets"),
        ("get", "/tickets/dashboard"),
        ("get", "/tickets/{ticket_id}/comments"),
        ("post", "/tickets/{ticket_id}/attachments"),
        ("delete", "/ticket-tags/{tag_id}"),
    }
    for method, path in queried:
        operation = schema["paths"][PREFIX + path][method]
        queries = [p for p in operation["parameters"] if p["in"] == "query"]
        assert queries, path
        for parameter in queries:
            assert parameter.get("description"), (path, parameter["name"])
    listed = {
        p["name"]
        for p in schema["paths"][PREFIX + "/tickets"]["get"]["parameters"]
        if p["in"] == "query"
    }
    assert listed == {
        "limit",
        "cursor",
        "q",
        "status",
        "priority",
        "tag_id",
        "assignee_id",
        "unassigned",
        "created_from",
        "created_to",
        "sort",
        "order",
    }


async def test_the_upload_is_documented_as_a_multipart_form_with_its_errors(client):
    schema = (await client.get("/openapi.json")).json()
    upload = schema["paths"][PREFIX + "/tickets/{ticket_id}/attachments"]["post"]
    form = upload["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert form["required"] == ["file"]
    assert form["properties"]["file"] == {"type": "string", "format": "binary"}
    assert {"400", "413", "503"} <= set(upload["responses"])
    assert upload["responses"]["201"]
    assert "2 MB" in upload["description"] and "ATTACHMENT_TOO_LARGE" in upload["description"]
    download = schema["paths"][PREFIX + "/tickets/{ticket_id}/attachments/{attachment_id}"]["get"]
    assert "application/octet-stream" in download["responses"]["200"]["content"]
    assert "nosniff" in download["description"]


async def test_the_response_shapes_are_the_documented_ones(client):
    schema = (await client.get("/openapi.json")).json()
    models = schema["components"]["schemas"]
    assert set(models["TicketDashboard"]["properties"]) == DASHBOARD_KEYS
    assert set(models["TicketListItem"]["properties"]) == ITEM_KEYS
    assert set(models["TicketDetail"]["properties"]) == DETAIL_KEYS
    assert set(models["Kpis"]["properties"]) == {
        "total",
        *STATUSES,
    }
    assert set(models["Kpi"]["properties"]) == {"value", "previous", "change"}
    assert set(models["Change"]["properties"]) == {"kind", "percent"}
    assert models["Change"]["properties"]["kind"]["enum"] == ["new", "percent", "none"]
    assert models["TicketListItem"]["properties"]["status"]["enum"] == STATUSES
    assert models["TicketListItem"]["properties"]["priority"]["enum"] == [
        "high",
        "medium",
        "low",
        "not_set",
    ]
    assert set(models["TagCount"]["properties"]) == {"tag_id", "name", "color", "count", "is_other"}
    assert set(models["AttachmentRead"]["properties"]) == {
        "id",
        "ticket_id",
        "comment_id",
        "file_name",
        "content_type",
        "size_bytes",
        "is_image",
        "uploaded_by",
        "uploaded_by_name",
        "created_at",
    }
    assert set(models["BulkResult"]["properties"]) == {"action", "affected", "changed"}


async def test_every_ticket_operation_has_a_unique_id(client):
    schema = (await client.get("/openapi.json")).json()
    ids = [
        operation["operationId"]
        for path, methods in schema["paths"].items()
        if "/ticket" in path
        for operation in methods.values()
    ]
    assert len(ids) == len(EXPECTED) == len(set(ids))

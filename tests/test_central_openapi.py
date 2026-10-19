"""The Clients 360 routes are documented in the OpenAPI schema: summaries, descriptions, error
responses, and a description on every parameter and field the UI builds on."""

PREFIX = "/api/v1/organizations/{organization_id}"

# (method, path, permission-protected, extra response codes)
NEW_ROUTES = [
    ("get", "/central-clients", True, {"503"}),
    ("post", "/central-clients/{central_id}/enable", True, {"409", "503"}),
    ("post", "/central-clients/events", True, set()),
    ("post", "/central-clients/process-changes", True, set()),
    ("get", "/central-clients/mock/availability", True, set()),
    ("post", "/central-clients/mock/availability", True, set()),
    ("post", "/central-clients/mock/changes", True, set()),
    ("post", "/clients/central-sync", True, set()),
    ("post", "/clients/{client_id}/central-sync", True, {"409"}),
    ("post", "/clients/{client_id}/central-link", True, {"409", "503"}),
    ("delete", "/clients/{client_id}/central-link", True, {"409"}),
    ("get", "/clients/{client_id}/overview", True, set()),
    ("get", "/integrations/sync-logs", True, set()),
    ("get", "/invoice-templates", True, set()),
    ("post", "/invoice-templates", True, {"409"}),
    ("get", "/invoice-templates/{template_id}", False, set()),
    ("patch", "/invoice-templates/{template_id}", True, {"409"}),
    ("delete", "/invoice-templates/{template_id}", True, {"409"}),
    ("post", "/invoice-templates/{template_id}/make-default", True, {"409"}),
]

NEW_SCHEMAS = [
    "CentralClientRead",
    "MatchingClient",
    "CentralSearchResult",
    "CentralEnabled",
    "CentralLinkRequest",
    "ClientSynced",
    "CentralSyncAll",
    "CentralEventRequest",
    "CentralEventResult",
    "ProcessedChanges",
    "MockAvailability",
    "MockAvailabilityUpdate",
    "MockChangeRequest",
    "MockChanged",
    "SyncLogRead",
    "InvoiceTemplateCreate",
    "InvoiceTemplateUpdate",
    "InvoiceTemplateRead",
    "ClientOverview",
    "JobsOverview",
    "TimeOverview",
    "ExpensesOverview",
    "WipOverview",
    "WipAgeing",
    "InvoicesOverview",
    "PaymentsOverview",
    "ActivityEvent",
    "TabCounts",
]

# Fields this release added to the existing client schemas.
NEW_CLIENT_FIELDS = {
    "ClientCreate": {"invoice_email", "default_service_id", "invoice_template_id"},
    "ClientUpdate": {"invoice_email", "default_service_id", "invoice_template_id"},
    "ClientDetail": {"default_service", "invoice_template", "merged_into"},
    "ClientListItem": {
        "invoice_email",
        "default_service_id",
        "invoice_template_id",
        "central_client_id",
        "central_sync_state",
        "central_synced_at",
        "merged_into_client_id",
    },
    "ClientSummary": {"by_central"},
}


async def load(client) -> dict:
    return (await client.get("/openapi.json")).json()


def has_description(prop: dict) -> bool:
    if prop.get("description"):
        return True
    return any(
        option.get("description") for option in prop.get("anyOf", []) + prop.get("allOf", [])
    )


async def test_the_new_routes_are_documented(client):
    spec = await load(client)
    for method, path, protected, extra in NEW_ROUTES:
        operation = spec["paths"][PREFIX + path][method]
        label = (method, path)
        assert operation["summary"], label
        assert len(operation["description"]) > 60, label
        codes = set(operation["responses"])
        assert {"401", "404", "422"} <= codes, label
        if protected:
            assert "403" in codes, label
        assert extra <= codes, label
        assert operation["tags"], label
    # The enable route says what 201 means; the search route its two sources.
    enable = spec["paths"][PREFIX + "/central-clients/{central_id}/enable"]["post"]
    assert enable["responses"]["201"]["description"]
    assert "CENTRAL_UNAVAILABLE" in spec["paths"][PREFIX + "/central-clients"]["get"]["description"]


async def test_every_parameter_of_the_new_routes_is_described(client):
    spec = await load(client)
    missing = []
    for method, path, *_ in NEW_ROUTES:
        for parameter in spec["paths"][PREFIX + path][method].get("parameters", []):
            if parameter["name"] == "organization_id":
                continue
            if not parameter.get("description"):
                missing.append(f"{method.upper()} {path} {parameter['name']}")
    assert not missing, missing


async def test_every_field_of_the_new_models_is_described(client):
    spec = await load(client)
    missing = []
    for name in NEW_SCHEMAS:
        schema = spec["components"]["schemas"][name]
        assert schema["properties"], name
        for field, prop in schema["properties"].items():
            if not has_description(prop):
                missing.append(f"{name}.{field}")
    assert not missing, missing


async def test_the_fields_added_to_the_client_models_are_described(client):
    spec = await load(client)
    missing = []
    for name, fields in NEW_CLIENT_FIELDS.items():
        properties = spec["components"]["schemas"][name]["properties"]
        for field in fields:
            assert field in properties, (name, field)
            if not has_description(properties[field]):
                missing.append(f"{name}.{field}")
    assert not missing, missing


async def test_the_client_list_filter_and_export_name_the_central_filter(client):
    spec = await load(client)
    for path in ("/clients", "/clients/export"):
        parameters = {p["name"]: p for p in spec["paths"][PREFIX + path]["get"]["parameters"]}
        central = parameters["central"]
        assert "linked" in central["description"] and "local" in central["description"]
        assert "linked" in str(central["schema"]) and "local" in str(central["schema"])


async def test_the_events_request_documents_every_event_shape(client):
    spec = await load(client)
    body = spec["components"]["schemas"]["CentralEventRequest"]
    assert set(body["required"]) == {"event_id", "type", "central_id"}
    assert set(body["properties"]["type"]["enum"]) == {"renamed", "archived", "restored", "merged"}
    assert {"into_central_id", "occurred_at", "name", "country"} <= set(body["properties"])
    # The central id format is stated once and reused by every route that takes one.
    pattern = body["properties"]["central_id"]["pattern"]
    assert (
        pattern
        == spec["components"]["schemas"]["CentralLinkRequest"]["properties"]["central_id"][
            "pattern"
        ]
    )
    path_parameter = spec["paths"][PREFIX + "/central-clients/{central_id}/enable"]["post"][
        "parameters"
    ]
    assert any(
        p["name"] == "central_id" and p["schema"]["pattern"] == pattern for p in path_parameter
    )


async def test_the_overview_documents_its_sections_and_their_permissions(client):
    spec = await load(client)
    overview = spec["components"]["schemas"]["ClientOverview"]["properties"]
    for section, permission in (
        ("jobs", "job.read"),
        ("time", "time.read_all"),
        ("expenses", "wip.read"),
        ("wip", "wip.read"),
        ("invoices", "invoice.read"),
        ("payments", "payment.read"),
    ):
        description = overview[section].get("description") or " ".join(
            option.get("description", "") for option in overview[section].get("anyOf", [])
        )
        assert permission in description, section
    ageing = spec["components"]["schemas"]["WipAgeing"]["properties"]
    assert set(ageing) == {"0_30", "31_60", "61_90", "90_plus"}

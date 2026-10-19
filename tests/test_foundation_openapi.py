"""The period-lock routes and the practice settings of this release are documented in the OpenAPI
schema: summaries, descriptions that name the error codes and the permission, the error
responses, and a description on every parameter and field the UI builds on."""

PREFIX = "/api/v1/organizations/{organization_id}"

# (method, path, permission, the error codes the description names)
LOCK_ROUTES = [
    ("get", "/period-locks", "period_lock.read", ["PERIOD_LOCKED"]),
    ("get", "/period-locks/history", "period_lock.read", ["limit", "cursor"]),
    (
        "post",
        "/period-locks",
        "period_lock.manage",
        ["PERIOD_LOCK_NOT_FORWARD", "PERIOD_LOCK_IN_FUTURE"],
    ),
    (
        "post",
        "/period-locks/reopen",
        "period_lock.reopen",
        ["PERIOD_LOCK_NOT_BACKWARD", "PERIOD_LOCK_REASON_REQUIRED"],
    ),
]
SETTINGS = (
    "fixed_fee_wip_mode",
    "wip_adjustment_approval_threshold",
    "receipt_required_threshold",
    "invoice_approval_required",
    "invoice_approval_threshold",
    "mileage_rate",
    "expense_markup_bp",
    "rate_mode",
)
SCHEMAS = (
    "PeriodLockRead",
    "PeriodLockEventRead",
    "PeriodLockCreate",
    "PeriodLockReopen",
    "OrganizationRead",
    "OrganizationUpdate",
)


async def load(client) -> dict:
    return (await client.get("/openapi.json")).json()


def has_description(prop: dict) -> bool:
    if prop.get("description"):
        return True
    return any(
        option.get("description") for option in prop.get("anyOf", []) + prop.get("allOf", [])
    )


def upper_bound(prop: dict) -> int | None:
    bounds = [prop.get("maximum")] + [o.get("maximum") for o in prop.get("anyOf", [])]
    return max((b for b in bounds if b is not None), default=None)


def choices(prop: dict) -> set[str]:
    """The values a field accepts, whether it is optional (``anyOf``) or not."""
    found = set(prop.get("enum", []))
    for option in prop.get("anyOf", []):
        found |= set(option.get("enum", []))
    return found


async def test_the_period_lock_routes_are_documented(client):
    spec = await load(client)
    for method, path, permission, mentions in LOCK_ROUTES:
        operation = spec["paths"][PREFIX + path][method]
        label = (method, path)
        assert operation["summary"], label
        assert operation["tags"] == ["Period locks"], label
        assert len(operation["description"]) > 60, label
        assert f"`{permission}`" in operation["description"], label
        assert {"401", "403", "404", "422"} <= set(operation["responses"]), label
        assert operation["responses"]["200"]["description"], label
        for text in mentions:
            assert text in operation["description"] or any(
                p["name"] == text for p in operation.get("parameters", [])
            ), (label, text)


async def test_every_parameter_of_the_period_lock_routes_is_described(client):
    spec = await load(client)
    missing = []
    for method, path, *_ in LOCK_ROUTES:
        for parameter in spec["paths"][PREFIX + path][method].get("parameters", []):
            if parameter["name"] != "organization_id" and not parameter.get("description"):
                missing.append(f"{method.upper()} {path} {parameter['name']}")
    assert not missing, missing


async def test_every_field_of_the_lock_and_organization_schemas_is_described(client):
    schemas = (await load(client))["components"]["schemas"]
    missing = []
    for name in SCHEMAS:
        for field, prop in schemas[name]["properties"].items():
            if not has_description(prop):
                missing.append(f"{name}.{field}")
    assert not missing, missing
    for name in ("OrganizationRead", "OrganizationUpdate"):
        assert set(SETTINGS) <= set(schemas[name]["properties"]), name


async def test_the_lock_request_models_say_what_is_required(client):
    schemas = (await load(client))["components"]["schemas"]
    assert schemas["PeriodLockCreate"]["required"] == ["locked_through"]
    # An empty body must never remove the lock: reopen_to is required, even when null.
    assert schemas["PeriodLockReopen"]["required"] == ["reopen_to"]
    reopen = schemas["PeriodLockReopen"]["properties"]
    assert "PERIOD_LOCK_REASON_REQUIRED" in reopen["reason"]["description"]
    assert "PERIOD_LOCK_NOT_BACKWARD" in reopen["reopen_to"]["description"]
    create = schemas["PeriodLockCreate"]["properties"]
    assert "PERIOD_LOCK_IN_FUTURE" in create["locked_through"]["description"]
    assert "PERIOD_LOCK_NOT_FORWARD" in create["locked_through"]["description"]


async def test_the_organization_routes_name_the_new_settings_and_their_bounds(client):
    spec = await load(client)
    update = spec["paths"][PREFIX]
    patch = update["patch"]
    for name in SETTINGS:
        assert f"`{name}`" in patch["description"], name
    assert {"401", "403", "404", "422"} <= set(patch["responses"])
    assert "`organization.update`" in patch["description"]
    assert "`organization.read`" in update["get"]["description"]

    properties = spec["components"]["schemas"]["OrganizationUpdate"]["properties"]
    assert upper_bound(properties["mileage_rate"]) == 1_000
    assert upper_bound(properties["expense_markup_bp"]) == 100_000
    for name in (
        "wip_adjustment_approval_threshold",
        "receipt_required_threshold",
        "invoice_approval_threshold",
    ):
        assert upper_bound(properties[name]) == 100_000_000, name
    assert choices(properties["rate_mode"]) == {"staff_first", "grade_only"}
    assert choices(properties["fixed_fee_wip_mode"]) == {"on_completion", "by_budget_progress"}
    read = spec["components"]["schemas"]["OrganizationRead"]["properties"]
    assert choices(read["rate_mode"]) == {"staff_first", "grade_only"}
    assert choices(read["fixed_fee_wip_mode"]) == {"on_completion", "by_budget_progress"}


async def test_the_sequence_routes_name_the_seven_kinds_and_the_forward_only_rule(client):
    spec = await load(client)
    listing = spec["paths"][PREFIX + "/sequences"]["get"]
    assert "estimate" in listing["description"]
    change = spec["paths"][PREFIX + "/sequences/{kind}"]["patch"]
    assert "SEQUENCE_BACKWARDS" in change["description"]
    kind = next(p for p in change["parameters"] if p["name"] == "kind")
    assert "estimate" in kind["schema"].get("enum", [])

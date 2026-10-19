"""The expense routes are documented in the OpenAPI schema, with the shapes the UI is built from."""

import pytest

from tests.test_expense_claims import ACTION_KEYS, CLAIM_KEYS, TOTAL_KEYS
from tests.test_expense_lines import LINE_KEYS
from tests.test_expense_receipts import ITEM_KEYS as RECEIPT_ITEM_KEYS
from tests.test_expense_receipts import RECEIPT_KEYS

PREFIX = "/api/v1/organizations/{organization_id}"
TYPES, CLAIMS, RECEIPTS = "Expense types", "Expense claims", "Expense receipts"
EXPECTED = {
    ("get", "/expense-types"): TYPES,
    ("post", "/expense-types"): TYPES,
    ("get", "/expense-types/{type_id}"): TYPES,
    ("patch", "/expense-types/{type_id}"): TYPES,
    ("delete", "/expense-types/{type_id}"): TYPES,
    ("get", "/expense-claims"): CLAIMS,
    ("post", "/expense-claims"): CLAIMS,
    ("get", "/expense-claims/summary"): CLAIMS,
    ("get", "/expense-claims/{claim_id}"): CLAIMS,
    ("patch", "/expense-claims/{claim_id}"): CLAIMS,
    ("delete", "/expense-claims/{claim_id}"): CLAIMS,
    ("post", "/expense-claims/{claim_id}/lines"): CLAIMS,
    ("patch", "/expense-claims/{claim_id}/lines/{line_id}"): CLAIMS,
    ("delete", "/expense-claims/{claim_id}/lines/{line_id}"): CLAIMS,
    ("post", "/expense-claims/{claim_id}/submit"): CLAIMS,
    ("post", "/expense-claims/{claim_id}/recall"): CLAIMS,
    ("post", "/expense-claims/{claim_id}/approve"): CLAIMS,
    ("post", "/expense-claims/{claim_id}/return"): CLAIMS,
    ("get", "/expense-approvals"): CLAIMS,
    ("post", "/expense-approvals/bulk-approve"): CLAIMS,
    ("get", "/expenses"): CLAIMS,
    ("post", "/expenses/{line_id}/receipts"): RECEIPTS,
    ("get", "/expenses/{line_id}/receipts/{receipt_id}"): RECEIPTS,
    ("delete", "/expenses/{line_id}/receipts/{receipt_id}"): RECEIPTS,
    ("get", "/expense-receipts"): RECEIPTS,
}
# The operations that can answer 409: the writes, the transitions and the period lock.
CONFLICTS = {
    ("post", "/expense-types"),
    ("patch", "/expense-types/{type_id}"),
    ("delete", "/expense-types/{type_id}"),
    ("post", "/expense-claims"),
    ("patch", "/expense-claims/{claim_id}"),
    ("delete", "/expense-claims/{claim_id}"),
    ("post", "/expense-claims/{claim_id}/lines"),
    ("patch", "/expense-claims/{claim_id}/lines/{line_id}"),
    ("delete", "/expense-claims/{claim_id}/lines/{line_id}"),
    ("post", "/expense-claims/{claim_id}/submit"),
    ("post", "/expense-claims/{claim_id}/recall"),
    ("post", "/expense-claims/{claim_id}/approve"),
    ("post", "/expense-claims/{claim_id}/return"),
    ("post", "/expense-approvals/bulk-approve"),
    ("post", "/expenses/{line_id}/receipts"),
    ("delete", "/expenses/{line_id}/receipts/{receipt_id}"),
}
PAGE = {"limit", "cursor"}
LISTS = {
    "/expense-types": PAGE | {"q", "is_active", "is_mileage", "sort", "order"},
    "/expense-claims": PAGE
    | {
        "status",
        "user_id",
        "mine",
        "date_from",
        "date_to",
        "client_id",
        "job_id",
        "is_billable",
        "q",
        "sort",
        "order",
    },
    "/expense-approvals": PAGE | {"user_id", "q"},
    "/expenses": PAGE
    | {
        "status",
        "claim_id",
        "user_id",
        "mine",
        "client_id",
        "job_id",
        "expense_type_id",
        "is_billable",
        "billing_status",
        "date_from",
        "date_to",
        "needs_receipt",
        "q",
        "sort",
        "order",
    },
    "/expense-receipts": PAGE
    | {
        "claim_id",
        "expense_id",
        "user_id",
        "mine",
        "client_id",
        "status",
        "date_from",
        "date_to",
        "q",
        "sort",
        "order",
    },
}


async def schema_of(client) -> dict:
    return (await client.get("/openapi.json")).json()


async def test_routes_are_documented(client):
    schema = await schema_of(client)
    for (method, path), tag in EXPECTED.items():
        operation = schema["paths"][PREFIX + path][method]
        assert operation["summary"], (method, path)
        assert len(operation["description"]) > 40, (method, path)
        assert tag in operation["tags"], (method, path)
        assert {"401", "403", "404", "422"} <= set(operation["responses"]), (method, path)
        assert ("409" in operation["responses"]) == ((method, path) in CONFLICTS), (method, path)


async def test_every_expense_operation_has_a_unique_id(client):
    schema = await schema_of(client)
    ids = [
        operation["operationId"]
        for path, methods in schema["paths"].items()
        if path.startswith(PREFIX + "/expense")
        for operation in methods.values()
    ]
    assert len(ids) == len(EXPECTED) == len(set(ids)) == 25


async def test_query_parameters_are_described(client):
    schema = await schema_of(client)
    for path, expected in LISTS.items():
        operation = schema["paths"][PREFIX + path]["get"]
        queries = [p for p in operation["parameters"] if p["in"] == "query"]
        assert {p["name"] for p in queries} == expected, path
        for parameter in queries:
            assert parameter.get("description"), (path, parameter["name"])
    # The path parameters are ids.
    parameters = schema["paths"][PREFIX + "/expense-claims/{claim_id}/lines/{line_id}"]["patch"][
        "parameters"
    ]
    assert {p["name"] for p in parameters if p["in"] == "path"} == {
        "organization_id",
        "claim_id",
        "line_id",
    }


async def test_the_upload_is_documented_as_a_multipart_form_with_its_errors(client):
    schema = await schema_of(client)
    upload = schema["paths"][PREFIX + "/expenses/{line_id}/receipts"]["post"]
    form = upload["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert form["required"] == ["file"]
    assert form["properties"]["file"] == {"type": "string", "format": "binary"}
    assert {"400", "403", "404", "409", "413", "503"} <= set(upload["responses"])
    assert upload["responses"]["201"]
    for text in ("10 MiB", "FILE_TOO_LARGE", "FILE_TYPE_NOT_ALLOWED", "RECEIPT_LIMIT_REACHED"):
        assert text in upload["description"], text
    assert "STORAGE_UNAVAILABLE" in upload["description"]
    download = schema["paths"][PREFIX + "/expenses/{line_id}/receipts/{receipt_id}"]["get"]
    assert "application/octet-stream" in download["responses"]["200"]["content"]
    assert (
        "nosniff" in download["description"] and "RECEIPT_FILE_MISSING" in download["description"]
    )


@pytest.mark.parametrize(
    ("method", "path", "codes"),
    [
        ("post", "/expense-claims/{claim_id}/submit", ("EXPENSE_CLAIM_EMPTY", "RECEIPT_REQUIRED")),
        (
            "post",
            "/expense-claims/{claim_id}/approve",
            ("SELF_APPROVAL_FORBIDDEN", "expense.approve"),
        ),
        (
            "post",
            "/expense-claims/{claim_id}/return",
            ("EXPENSE_RETURN_REASON_REQUIRED", "SELF_APPROVAL_FORBIDDEN"),
        ),
        (
            "patch",
            "/expense-claims/{claim_id}/lines/{line_id}",
            ("EXPENSE_CORRECTION_REASON_REQUIRED", "EXPENSE_BILLED", "EXPENSE_CLAIM_LOCKED"),
        ),
        ("post", "/expense-claims/{claim_id}/lines", ("EXPENSE_CLAIM_LINE_LIMIT", "JOB_CLOSED")),
        ("delete", "/expense-claims/{claim_id}", ("EXPENSE_CLAIM_LOCKED",)),
        ("post", "/expense-approvals/bulk-approve", ("PERIOD_LOCKED", "100")),
        ("post", "/expense-claims", ("100", "period lock")),
        ("delete", "/expense-types/{type_id}", ("EXPENSE_TYPE_IN_USE",)),
    ],
)
async def test_the_descriptions_name_the_rules_and_their_error_codes(client, method, path, codes):
    operation = (await schema_of(client))["paths"][PREFIX + path][method]
    for code in codes:
        assert code in operation["description"], (path, code)


async def test_the_response_shapes_are_the_documented_ones(client):
    models = (await schema_of(client))["components"]["schemas"]

    def properties(name: str) -> set[str]:
        return set(models[name]["properties"])

    assert properties("ExpenseClaimRead") == CLAIM_KEYS
    assert properties("ExpenseClaimDetail") == CLAIM_KEYS | {"lines", "events"}
    assert properties("ClaimTotals") == TOTAL_KEYS
    assert properties("ClaimActions") == ACTION_KEYS
    assert properties("ExpenseLineRead") == LINE_KEYS
    assert properties("ExpenseLineListItem") == LINE_KEYS | {
        "claim_status",
        "claim_title",
        "user_id",
        "user_name",
    }
    assert properties("ReceiptRead") == RECEIPT_KEYS
    assert properties("ReceiptListItem") == RECEIPT_ITEM_KEYS
    assert properties("ClaimEventRead") == {
        "id",
        "action",
        "actor_user_id",
        "actor_name",
        "note",
        "created_at",
    }
    assert properties("ExpenseSummary") == {"counts", "pending_gross", "awaiting_my_approval"}
    assert properties("ClaimStatusCounts") == {
        "draft",
        "submitted",
        "approved",
        "returned",
        "total",
    }
    assert properties("ApprovalBadge") == {"count", "gross"}
    assert properties("ExpenseTypeRead") == {
        "id",
        "name",
        "code",
        "default_vat_rate_id",
        "is_mileage",
        "default_billable",
        "is_active",
        "is_used",
        "can_delete",
        "created_at",
        "updated_at",
    }
    assert models["ExpenseClaimRead"]["properties"]["status"]["enum"] == [
        "draft",
        "submitted",
        "approved",
        "returned",
    ]
    assert models["ClaimEventRead"]["properties"]["action"]["enum"] == [
        "submitted",
        "recalled",
        "approved",
        "returned",
        "corrected",
    ]
    assert models["ExpenseLineRead"]["properties"]["payer"]["enum"] == ["staff", "practice"]


async def test_the_request_bodies_are_the_documented_ones(client):
    models = (await schema_of(client))["components"]["schemas"]

    def properties(name: str) -> set[str]:
        return set(models[name]["properties"])

    line = {
        "expense_type_id",
        "incurred_on",
        "description",
        "client_id",
        "job_id",
        "supplier",
        "vat_rate_id",
        "net",
        "vat",
        "gross",
        "miles",
        "is_billable",
        "recharge_amount",
        "payer",
    }
    assert properties("ExpenseLineCreate") == line
    assert properties("ExpenseLineUpdate") == line | {"reason"}
    assert models["ExpenseLineCreate"]["required"] == [
        "expense_type_id",
        "incurred_on",
        "description",
    ]
    assert properties("ExpenseClaimCreate") == {"title", "user_id", "lines"}
    assert properties("ExpenseClaimUpdate") == {"title"}
    assert properties("ClaimApprove") == {"note"}
    assert properties("ClaimReturn") == {"reason"}
    assert properties("ExpenseBulkApprove") == {"claim_ids"}
    assert models["ExpenseBulkApprove"]["required"] == ["claim_ids"]
    assert models["ExpenseBulkApprove"]["properties"]["claim_ids"]["maxItems"] == 100
    assert models["ExpenseClaimCreate"]["properties"]["lines"]["maxItems"] == 100
    type_body = {
        "name",
        "code",
        "default_vat_rate_id",
        "is_mileage",
        "default_billable",
        "is_active",
    }
    assert properties("ExpenseTypeCreate") == type_body == properties("ExpenseTypeUpdate")
    assert models["ExpenseTypeCreate"]["required"] == ["name", "code"]

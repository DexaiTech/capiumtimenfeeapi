"""Central exception handling: every error leaves as the same envelope."""

from typing import Any

from asyncpg.exceptions import (
    CheckViolationError,
    DeadlockDetectedError,
    ExclusionViolationError,
    ForeignKeyViolationError,
    SerializationError,
    UniqueViolationError,
)
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import request_id_var
from app.core.exceptions import AppException
from app.core.logging import get_logger

log = get_logger(__name__)

# Constraint name → (field, message). Constraint names never reach a client.
UNIQUE_MESSAGES: dict[str, tuple[str, str]] = {
    "uq_users_email_lower": ("email", "This email is already in use."),
    "uq_organizations_slug": ("slug", "This slug is already taken."),
    "uq_roles_organization_id_name_lower": ("name", "A role with this name already exists."),
    "uq_organization_members_organization_id_initials": (
        "initials",
        "These initials are already used by someone else in the practice.",
    ),
    "uq_vat_rates_organization_id_name_lower": ("name", "A VAT rate with this name exists."),
    "uq_vat_rates_organization_id_default": ("is_default", "Another VAT rate is the default."),
    "uq_services_organization_id_code": ("code", "A service with this code already exists."),
    "uq_services_organization_id_name_lower": (
        "name",
        "A service with this name already exists.",
    ),
    "uq_activity_codes_organization_id_code": (
        "code",
        "An activity code with this code already exists.",
    ),
    "uq_activity_codes_organization_id_name_lower": (
        "name",
        "An activity code with this name already exists.",
    ),
    "uq_grades_organization_id_code": ("code", "A grade with this code already exists."),
    "uq_grades_organization_id_name_lower": ("name", "A grade with this name already exists."),
    "uq_organization_members_organization_id_staff_code": (
        "code",
        "Another staff member already has this code.",
    ),
    "uq_organization_members_organization_id_work_email_active": (
        "email",
        "Another active staff member already uses this email address.",
    ),
    "uq_clients_organization_id_code": ("code", "Another client already uses this code."),
    "uq_client_contacts_client_id_primary": ("is_primary", "This client has a primary contact."),
    "uq_client_services_client_id_service_id": (
        "service_id",
        "This client already takes this service.",
    ),
    "uq_jobs_organization_id_number": ("number", "Another job already has this number."),
    "uq_timesheets_organization_id_user_id_week_start": (
        "week_start",
        "A timesheet for this week already exists.",
    ),
    "uq_timers_organization_id_user_id": ("timer", "A timer is already running."),
    "uq_invoices_organization_id_number": ("number", "This invoice number is already used."),
    "uq_credit_notes_organization_id_number": ("number", "This credit note number is used."),
    "uq_wip_allocations_time_entry_live": ("items", "This time is already being billed."),
    "uq_wip_allocations_disbursement_live": (
        "items",
        "This disbursement is already being billed.",
    ),
    "uq_wip_allocations_expense_live": ("items", "This expense is already being billed."),
    "uq_payment_allocations_payment_id_invoice_id": (
        "invoice_id",
        "This payment is already allocated to this invoice.",
    ),
    "uq_ticket_tags_organization_id_name_lower": (
        "name",
        "A tag with this name already exists.",
    ),
    "uq_tickets_organization_id_code": ("code", "Another ticket already has this code."),
    "uq_tickets_organization_id_number": ("number", "Another ticket already has this number."),
    "uq_invoice_templates_organization_id_name_lower": (
        "name",
        "A template with this name already exists.",
    ),
    "uq_invoice_templates_organization_id_default": (
        "is_default",
        "Another template is the default.",
    ),
    "uq_clients_organization_id_central_client_id": (
        "central_id",
        "This central client is already linked to a client of the practice.",
    ),
    "uq_expense_types_organization_id_name_lower": (
        "name",
        "An active expense type with this name already exists.",
    ),
    "uq_expense_types_organization_id_code": (
        "code",
        "An expense type with this code already exists.",
    ),
}

_STATUS_CODES = {
    400: "VALIDATION_ERROR",
    401: "AUTH_REQUIRED",
    403: "PERMISSION_DENIED",
    404: "RESOURCE_NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    413: "FILE_TOO_LARGE",
    415: "FILE_TYPE_NOT_ALLOWED",
    429: "RATE_LIMITED",
}
_STATUS_MESSAGES = {
    404: "Not found.",
    405: "Method not allowed.",
}


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "request_id": request_id_var.get(),
            },
        },
        headers=headers,
    )


def _validation_details(errors: Any) -> list[dict[str, str]]:
    """Pydantic errors → ``{field, code, message}``. The submitted input is not echoed."""
    details = []
    for err in errors:
        loc = [str(part) for part in err.get("loc", ()) if part not in ("body",)]
        message = str(err.get("msg", "Invalid value."))
        message = message.removeprefix("Value error, ")
        field = ".".join(loc)
        # Model-level validators name their field as a "field: message" prefix.
        if not field and ": " in message:
            candidate, _, rest = message.partition(": ")
            if candidate.isidentifier():
                field, message = candidate, rest
        details.append(
            {"field": field or "body", "code": str(err.get("type", "invalid")), "message": message}
        )
    return details


async def _app_exception(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppException)
    if exc.status_code >= 500:
        log.error("app_error", code=exc.code, exc_info=exc)
    elif exc.status_code in (403, 429):
        log.warning("request_denied", code=exc.code)
    return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def _request_validation(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(
        422, "VALIDATION_ERROR", "Some fields are invalid.", _validation_details(exc.errors())
    )


async def _http_exception(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_CODES.get(
        exc.status_code, "INTERNAL_ERROR" if exc.status_code >= 500 else "VALIDATION_ERROR"
    )
    message = _STATUS_MESSAGES.get(exc.status_code) or (
        str(exc.detail) if exc.status_code < 500 else AppException.message
    )
    return error_response(exc.status_code, code, message, headers=dict(exc.headers or {}))


def _unavailable() -> JSONResponse:
    return error_response(
        503,
        "SERVICE_UNAVAILABLE",
        "The service is temporarily unavailable. Please try again shortly.",
        headers={"Retry-After": "5"},
    )


async def _database_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DBAPIError | PoolTimeoutError)
    if isinstance(exc, PoolTimeoutError | OperationalError | InterfaceError):
        log.error("database_unavailable", exc_info=exc)
        return _unavailable()
    # SQLAlchemy wraps the asyncpg error; the original carries the SQLSTATE class.
    cause = exc.orig.__cause__ if exc.orig is not None else None
    if isinstance(cause, UniqueViolationError):
        field, message = UNIQUE_MESSAGES.get(
            cause.constraint_name or "", ("", "This value is already in use.")
        )
        details = [{"field": field, "code": "duplicate", "message": message}] if field else None
        return error_response(409, "CONFLICT", message, details)
    if isinstance(cause, ExclusionViolationError):
        if cause.constraint_name == "ex_rate_rules_scope_period":
            return error_response(
                409,
                "RATE_OVERLAP",
                "Another rate of the same kind already covers part of this period.",
            )
        return error_response(409, "CONFLICT", "This overlaps with an existing record.")
    if isinstance(cause, ForeignKeyViolationError):
        return error_response(422, "VALIDATION_ERROR", "A referenced item does not exist.")
    if isinstance(cause, CheckViolationError):
        log.warning("check_violation", constraint=cause.constraint_name)
        return error_response(422, "VALIDATION_ERROR", "Some fields are invalid.")
    if isinstance(cause, SerializationError | DeadlockDetectedError):
        return _unavailable()
    if isinstance(exc, IntegrityError):
        log.error("integrity_error", exc_info=exc)
    else:
        log.error("database_error", exc_info=exc)
    return error_response(500, AppException.code, AppException.message)


async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
    # The stack goes to the log, never to the client.
    log.error("unhandled_exception", exc_info=exc)
    return error_response(500, AppException.code, AppException.message)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppException, _app_exception)
    app.add_exception_handler(RequestValidationError, _request_validation)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(DBAPIError, _database_error)
    app.add_exception_handler(PoolTimeoutError, _database_error)
    app.add_exception_handler(Exception, _unexpected)

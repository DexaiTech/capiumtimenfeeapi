"""The notification catalogue: what can notify a person and each type's default channels.

Adding a type means adding it here and a template of the same key to
``app.services.template_catalogue`` (a test refuses a type without one). The database stores
the code as text, so no migration is needed.
"""

from enum import StrEnum
from typing import NamedTuple

CHANNELS = ("in_app", "email", "sms")


class NotificationType(StrEnum):
    TIMESHEET_REMINDER = "timesheet_reminder"
    TIMESHEET_RETURNED = "timesheet_returned"
    APPROVAL_PENDING = "approval_pending"
    BUDGET_ALERT = "budget_alert"
    INVOICE_DELIVERY = "invoice_delivery"
    PAYMENT_REMINDER = "payment_reminder"
    FAILED_JOB = "failed_job"
    EXPENSE_PENDING = "expense_pending"
    EXPENSE_APPROVED = "expense_approved"
    EXPENSE_RETURNED = "expense_returned"
    INVOICE_APPROVAL_PENDING = "invoice_approval_pending"
    INVOICE_APPROVED = "invoice_approved"
    INVOICE_REJECTED = "invoice_rejected"
    PAYMENT_REVERSED = "payment_reversed"


class NotificationTypeDef(NamedTuple):
    code: str
    label: str
    description: str
    # Defaults of a practice that has not changed them, which a person inherits.
    in_app: bool = True
    email: bool = True
    sms: bool = False


CATALOGUE: tuple[NotificationTypeDef, ...] = (
    NotificationTypeDef(
        NotificationType.TIMESHEET_REMINDER,
        "Timesheet reminder",
        "A reminder to submit a timesheet that is still open.",
    ),
    NotificationTypeDef(
        NotificationType.TIMESHEET_RETURNED,
        "Timesheet returned",
        "Your timesheet was returned to you for changes.",
    ),
    NotificationTypeDef(
        NotificationType.APPROVAL_PENDING,
        "Approval pending",
        "A timesheet is waiting for you to approve it.",
    ),
    NotificationTypeDef(
        NotificationType.BUDGET_ALERT,
        "Budget alert",
        "A job has used a set share of its time or fee budget.",
    ),
    NotificationTypeDef(
        NotificationType.INVOICE_DELIVERY,
        "Invoice delivery",
        "An invoice you sent was emailed to the client.",
        email=False,
    ),
    NotificationTypeDef(
        NotificationType.PAYMENT_REMINDER,
        "Payment reminder",
        "An invoice is overdue and needs chasing.",
    ),
    NotificationTypeDef(
        NotificationType.FAILED_JOB,
        "Failed job",
        "A background job, such as sending an invoice, failed after its retries.",
    ),
    NotificationTypeDef(
        NotificationType.EXPENSE_PENDING,
        "Expense claim pending",
        "An expense claim is waiting for you to approve it.",
    ),
    NotificationTypeDef(
        NotificationType.EXPENSE_APPROVED,
        "Expense claim approved",
        "Your expense claim was approved.",
    ),
    NotificationTypeDef(
        NotificationType.EXPENSE_RETURNED,
        "Expense claim returned",
        "Your expense claim was returned to you for changes.",
    ),
    NotificationTypeDef(
        NotificationType.INVOICE_APPROVAL_PENDING,
        "Invoice approval pending",
        "An invoice is waiting for you to approve it.",
    ),
    NotificationTypeDef(
        NotificationType.INVOICE_APPROVED,
        "Invoice approved",
        "An invoice you prepared was approved and can be issued.",
    ),
    NotificationTypeDef(
        NotificationType.INVOICE_REJECTED,
        "Invoice rejected",
        "An invoice you prepared was rejected and is a draft again.",
    ),
    NotificationTypeDef(
        NotificationType.PAYMENT_REVERSED,
        "Payment reversed",
        "A payment from a client was reversed, for example because it bounced.",
    ),
)

BY_CODE: dict[str, NotificationTypeDef] = {entry.code: entry for entry in CATALOGUE}


class UnknownNotificationTypeError(ValueError):
    """A caller used a type that is not in the catalogue (a programming error, not user input)."""


def get_type(code: str) -> NotificationTypeDef:
    try:
        return BY_CODE[str(code)]
    except KeyError as exc:
        raise UnknownNotificationTypeError(f"unknown notification type {code!r}") from exc

"""The permission catalogue and the permission sets of the system roles.

Permissions are seeded into the ``permissions`` table by migration. Adding one means
adding it here *and* writing a migration that inserts it (see ``migration_helpers``).
"""

from collections.abc import Iterable
from typing import NamedTuple


class PermissionDef(NamedTuple):
    code: str
    category: str
    description: str


CATALOGUE: tuple[PermissionDef, ...] = (
    # Practice administration
    PermissionDef("organization.read", "Practice", "View practice details"),
    PermissionDef("organization.update", "Practice", "Change practice settings and numbering"),
    PermissionDef("organization.delete", "Practice", "Delete the practice"),
    PermissionDef("member.read", "Staff", "View the staff list"),
    PermissionDef("member.invite", "Staff", "Add staff accounts"),
    PermissionDef("member.update", "Staff", "Change a staff member's role, status and details"),
    PermissionDef("member.remove", "Staff", "Remove staff"),
    PermissionDef("role.read", "Roles", "View roles and their permissions"),
    PermissionDef("role.manage", "Roles", "Create, edit and delete custom roles"),
    PermissionDef("audit_log.read", "Audit", "View the audit log"),
    PermissionDef("settings.manage", "Practice", "Manage services, activity codes and VAT rates"),
    # Rates: cost rates are commercially sensitive, so reading them is separate.
    PermissionDef("rate.read", "Rates", "View charge-out and cost rates"),
    PermissionDef("rate.manage", "Rates", "Set staff and client rates"),
    # Clients
    PermissionDef("client.read", "Clients", "View clients and contacts"),
    PermissionDef("client.create", "Clients", "Create clients"),
    PermissionDef("client.update", "Clients", "Edit clients and contacts"),
    PermissionDef("client.delete", "Clients", "Archive and delete clients"),
    # Jobs
    PermissionDef("job.read", "Jobs", "View jobs"),
    PermissionDef("job.create", "Jobs", "Create jobs"),
    PermissionDef("job.update", "Jobs", "Edit jobs, budgets and assignees"),
    PermissionDef("job.delete", "Jobs", "Delete jobs"),
    # Time
    PermissionDef("time.log", "Time", "Log own time and run a timer"),
    PermissionDef("time.read_all", "Time", "View other people's time"),
    PermissionDef("time.manage_all", "Time", "Edit other people's unbilled time"),
    PermissionDef("timesheet.approve", "Time", "Approve and reject timesheets"),
    PermissionDef("timesheet.exempt", "Time", "Own time needs no approval before it is billed"),
    # Disbursements
    PermissionDef("disbursement.log", "Disbursements", "Record disbursements"),
    PermissionDef("disbursement.manage", "Disbursements", "Edit and delete any disbursement"),
    # Work in progress and billing
    PermissionDef("wip.read", "Billing", "View work in progress"),
    PermissionDef("wip.write_off", "Billing", "Write off work in progress"),
    PermissionDef("invoice.read", "Billing", "View invoices and credit notes"),
    PermissionDef("invoice.create", "Billing", "Prepare draft invoices"),
    PermissionDef("invoice.issue", "Billing", "Issue, send and void invoices"),
    PermissionDef("credit_note.create", "Billing", "Raise credit notes"),
    PermissionDef("payment.read", "Billing", "View payments received"),
    PermissionDef("payment.record", "Billing", "Record and allocate payments"),
    # Reporting
    PermissionDef("report.read", "Reports", "View time and job reports"),
    PermissionDef("report.financial", "Reports", "View WIP, debtors, billing and profit reports"),
    # Tickets: the practice's own bug and enhancement tracker ("Application Testing").
    PermissionDef(
        "ticket.read", "Tickets", "View tickets, comments, attachments and the dashboard"
    ),
    PermissionDef("ticket.create", "Tickets", "Raise tickets"),
    PermissionDef("ticket.update", "Tickets", "Edit tickets, tags, comments and attachments"),
    PermissionDef("ticket.delete", "Tickets", "Delete tags and moderate comments"),
    # Period lock
    PermissionDef("period_lock.read", "Period lock", "View period locks and their history"),
    PermissionDef(
        "period_lock.manage",
        "Period lock",
        "Lock a month, quarter or year of time, expenses and WIP",
    ),
    PermissionDef("period_lock.reopen", "Period lock", "Unlock a locked period"),
    # Expenses
    PermissionDef("expense.log", "Expenses", "Record and submit own expense claims"),
    PermissionDef("expense.read_all", "Expenses", "View everyone's expense claims and receipts"),
    PermissionDef("expense.approve", "Expenses", "Approve and return expense claims"),
    PermissionDef("expense.manage", "Expenses", "Manage expense types and edit any claim"),
    # More work in progress and billing
    PermissionDef("wip.adjust", "Billing", "Write work in progress on or off and reverse it"),
    PermissionDef("wip.approve", "Billing", "Approve WIP adjustments above the threshold"),
    PermissionDef("wip.transfer", "Billing", "Transfer WIP between jobs of a client"),
    PermissionDef("invoice.approve", "Billing", "Approve invoices awaiting approval"),
    PermissionDef("estimate.read", "Billing", "View estimates"),
    PermissionDef("estimate.manage", "Billing", "Create, send, convert and delete estimates"),
    PermissionDef("recurring.read", "Billing", "View recurring invoice schedules and their runs"),
    PermissionDef(
        "recurring.manage", "Billing", "Manage recurring invoice schedules and retry runs"
    ),
    PermissionDef("opening_balance.import", "Billing", "Import and manage opening balances"),
    # Exports, integrations and data tools
    PermissionDef("report.export", "Reports", "Download reports as CSV, Excel or PDF"),
    PermissionDef("audit_log.export", "Audit", "Export the audit log"),
    PermissionDef("integration.read", "Integrations", "View integration status and sync logs"),
    PermissionDef(
        "integration.manage", "Integrations", "Configure integrations, mappings and retries"
    ),
    PermissionDef("data.import_export", "Practice", "Import and export staff, rates and jobs"),
    PermissionDef("onboarding.manage", "Practice", "Work through the onboarding checklist"),
)

ALL_CODES: frozenset[str] = frozenset(p.code for p in CATALOGUE)

_ADMIN = ALL_CODES - {"organization.delete"}
_PARTNER = ALL_CODES - {
    "organization.delete",
    "role.manage",
    "period_lock.reopen",
    "integration.manage",
    "data.import_export",
}
_MANAGER = frozenset(
    {
        "organization.read",
        "member.read",
        "role.read",
        "rate.read",
        "client.read",
        "client.create",
        "client.update",
        "job.read",
        "job.create",
        "job.update",
        "time.log",
        "time.read_all",
        "time.manage_all",
        "timesheet.approve",
        "disbursement.log",
        "disbursement.manage",
        "wip.read",
        "invoice.read",
        "invoice.create",
        "payment.read",
        "report.read",
        "report.financial",
        "ticket.read",
        "ticket.create",
        "ticket.update",
        "ticket.delete",
        "period_lock.read",
        "expense.log",
        "expense.read_all",
        "expense.approve",
        "wip.adjust",
        "wip.transfer",
        "estimate.read",
        "estimate.manage",
        "recurring.read",
        "report.export",
    }
)
_STAFF = frozenset(
    {
        "organization.read",
        "member.read",
        "client.read",
        "job.read",
        "time.log",
        "disbursement.log",
        "report.read",
        "ticket.read",
        "ticket.create",
        "ticket.update",
        "expense.log",
    }
)
_BILLING = frozenset(
    {
        "organization.read",
        "member.read",
        "client.read",
        "client.update",
        "job.read",
        "time.read_all",
        "disbursement.log",
        "disbursement.manage",
        "wip.read",
        "invoice.read",
        "invoice.create",
        "invoice.issue",
        "credit_note.create",
        "payment.read",
        "payment.record",
        "report.read",
        "report.financial",
        "ticket.read",
        "ticket.create",
        "ticket.update",
        "period_lock.read",
        "expense.read_all",
        "estimate.read",
        "estimate.manage",
        "recurring.read",
        "recurring.manage",
        "opening_balance.import",
        "report.export",
    }
)
_VIEWER = frozenset(
    {
        "organization.read",
        "member.read",
        "client.read",
        "job.read",
        "time.read_all",
        "wip.read",
        "invoice.read",
        "payment.read",
        "report.read",
        "ticket.read",
        "estimate.read",
        "recurring.read",
    }
)


class SystemRole(NamedTuple):
    key: str
    name: str
    description: str
    permissions: frozenset[str]


SYSTEM_ROLES: tuple[SystemRole, ...] = (
    SystemRole("owner", "Owner", "Full control, including deleting the practice.", ALL_CODES),
    SystemRole("admin", "Admin", "Runs the practice: staff, roles, settings and billing.", _ADMIN),
    SystemRole(
        "partner", "Partner", "Owns client relationships, billing and write-offs.", _PARTNER
    ),
    SystemRole("manager", "Manager", "Runs jobs, approves time and prepares bills.", _MANAGER),
    SystemRole("staff", "Staff", "Records time and disbursements on their work.", _STAFF),
    SystemRole(
        "billing", "Billing", "Credit control: invoices, credit notes and payments.", _BILLING
    ),
    SystemRole("viewer", "Viewer", "Read-only access to clients, jobs, time and billing.", _VIEWER),
)

OWNER_KEY = "owner"


def effective_permissions(
    role_codes: Iterable[str], overrides: Iterable[tuple[str, str]]
) -> frozenset[str]:
    """A person's permissions: their role's, plus what is granted to them, less what is denied.

    ``overrides`` are ``(code, effect)`` pairs, effect ``grant`` or ``deny``. A denial wins over
    the role and over a grant of the same code (there is at most one override per code).
    """
    codes = set(role_codes)
    for code, effect in overrides:
        if effect == "grant":
            codes.add(code)
        else:
            codes.discard(code)
    return frozenset(codes)

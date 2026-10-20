"""What the audit trail knows about each kind of entry: its label, who may read a record's
history, and which details are commercially sensitive.

The trail stores what each service wrote (``action``, ``target_type``, ``target_id`` and a free
``metadata`` object); this module is how that is presented. An action or record type that is not
listed still works: its label is made from the action, and only ``audit_log.read`` opens the
history of a record type that is not in ``RECORD_READ_PERMISSIONS``.
"""

import re
from typing import Any

# The permission that reads the audit trail of the whole practice, and the one that exports it.
READ_PERMISSION = "audit_log.read"
EXPORT_PERMISSION = "audit_log.export"
# Without it nobody sees cost rates, cost values or margins, in the trail either.
COST_PERMISSION = "rate.read"

# Actions whose generated label ("Client contact created") would read badly.
ACTION_LABELS: dict[str, str] = {
    "auth.registered": "Account registered",
    "auth.email_verified": "Email address verified",
    "auth.login": "Signed in",
    "auth.login_failed": "Failed sign-in",
    "auth.logout": "Signed out",
    "auth.logout_all": "Signed out of every device",
    "auth.refresh_reuse_detected": "Session revoked after its token was reused",
    "auth.password_reset_requested": "Password reset requested",
    "auth.password_reset": "Password reset",
    "auth.password_changed": "Password changed",
    "auth.session_revoked": "Session revoked",
    "audit_log.exported": "Audit log exported",
    "organization.created": "Practice created",
    "organization.updated": "Practice settings updated",
    "organization.deleted": "Practice deleted",
    "member.created": "Staff member added",
    "member.updated": "Staff details updated",
    "member.role_changed": "Role changed",
    "member.suspended": "Staff member suspended",
    "member.reactivated": "Staff member reactivated",
    "member.removed": "Staff member removed",
    "member.password_reset": "Password reset by an administrator",
    "staff.login_linked": "Staff login linked",
    "staff.login_unlinked": "Staff login unlinked",
    "staff.invited": "Staff login invitation sent",
    "staff.left": "Staff member left",
    "role.permissions_changed": "Role permissions changed",
    "client.created": "Client created",
    "client.updated": "Client details updated",
    "client.archived": "Client archived",
    "client.deleted": "Client deleted",
    "client.imported": "Clients imported",
    "client.central_enabled": "Enabled from the central client service",
    "client.central_linked": "Linked to the central client service",
    "client.central_unlinked": "Unlinked from the central client service",
    "client.central_synced": "Synchronised with the central client service",
    "client.central_renamed": "Renamed by the central client service",
    "client.central_archived": "Archived by the central client service",
    "client.central_restored": "Restored by the central client service",
    "client.central_merged": "Merged with another client",
    "client_contact.created": "Contact added",
    "client_contact.updated": "Contact updated",
    "client_contact.deleted": "Contact removed",
    "client_service.created": "Service added",
    "client_service.updated": "Service updated",
    "client_service.deleted": "Service removed",
    "client_rate.created": "Client rate added",
    "client_rate.updated": "Client rate changed",
    "client_rate.deleted": "Client rate removed",
    "staff_rate.created": "Staff rate added",
    "staff_rate.deleted": "Staff rate removed",
    "rate.created": "Rate added",
    "rate.updated": "Rate changed",
    "rate.deleted": "Rate removed",
    "rate.rerun": "Rates re-run over time entries",
    "time_entries.revalued": "Time entries re-valued",
    "job.created": "Job created",
    "job.updated": "Job updated",
    "job.status_changed": "Job status changed",
    "job.assignees_changed": "Job assignees changed",
    "job.copied": "Job copied",
    "job.deleted": "Job deleted",
    "job.budget_alert": "Job budget alert raised",
    "job.tasks_reordered": "Job tasks reordered",
    "task.status_changed": "Task status changed",
    "task.assignees_changed": "Task assignees changed",
    "time_entry.created": "Time recorded",
    "time_entry.updated": "Time entry changed",
    "time_entry.deleted": "Time entry deleted",
    "time_entry.copied": "Time entry copied",
    "time_entry.week_copied": "Week of time copied",
    "time_entry.corrected": "Approved time corrected",
    "timesheet.submitted": "Timesheet submitted",
    "timesheet.recalled": "Timesheet recalled",
    "timesheet.approved": "Timesheet approved",
    "timesheet.rejected": "Timesheet rejected",
    "timesheet.reopened": "Timesheet reopened",
    "timesheet.reminders_sent": "Timesheet reminders sent",
    "disbursement.created": "Disbursement recorded",
    "disbursement.updated": "Disbursement changed",
    "disbursement.deleted": "Disbursement deleted",
    "expense_claim.submitted": "Expense claim submitted",
    "expense_claim.recalled": "Expense claim recalled",
    "expense_claim.returned": "Expense claim returned",
    "expense_claim.approved": "Expense claim approved",
    "expense.created": "Expense added",
    "expense.updated": "Expense changed",
    "expense.corrected": "Approved expense corrected",
    "expense.deleted": "Expense deleted",
    "expense_receipt.added": "Receipt attached",
    "expense_receipt.deleted": "Receipt removed",
    "wip.written_off": "WIP written off",
    "wip.write_off_reversed": "WIP write-off reversed",
    "invoice.draft_created": "Draft invoice created",
    "invoice.wip_added": "WIP added to an invoice",
    "invoice.updated": "Invoice updated",
    "invoice.line_added": "Invoice line added",
    "invoice.line_updated": "Invoice line changed",
    "invoice.line_removed": "Invoice line removed",
    "invoice.draft_deleted": "Draft invoice deleted",
    "invoice.issued": "Invoice issued",
    "invoice.voided": "Invoice voided",
    "invoice.send_queued": "Invoice emailed",
    "credit_note.issued": "Credit note issued",
    "estimate.created": "Estimate created",
    "estimate.updated": "Estimate updated",
    "estimate.sent": "Estimate sent",
    "estimate.accepted": "Estimate accepted",
    "estimate.declined": "Estimate declined",
    "estimate.converted": "Estimate converted to an invoice",
    "estimate.deleted": "Estimate deleted",
    "recurring.created": "Recurring schedule created",
    "recurring.updated": "Recurring schedule updated",
    "recurring.paused": "Recurring schedule paused",
    "recurring.resumed": "Recurring schedule resumed",
    "recurring.deleted": "Recurring schedule deleted",
    "recurring.run": "Recurring invoice drafted",
    "recurring.run_failed": "Recurring invoice failed",
    "payment.recorded": "Payment recorded",
    "payment.allocated": "Payment allocated",
    "payment.unallocated": "Payment allocation removed",
    "payment.voided": "Payment voided",
    "period_lock.locked": "Period locked",
    "period_lock.reopened": "Period reopened",
    "sequence.updated": "Numbering updated",
    "service.activity_codes_changed": "Service activity codes changed",
    "invoice_template.made_default": "Invoice template made the default",
    "email_template.reset": "Email template reset to the standard wording",
    "notification_settings.updated": "Notification settings updated",
    "ticket.status_changed": "Ticket status changed",
    "ticket_attachment.added": "Ticket file attached",
    "ticket_attachment.deleted": "Ticket file removed",
}

# The permissions of which any one lets a person read the history of a record of that type
# (``GET /audit-logs/history``). A person with ``audit_log.read`` reads the history of every
# record. Types that are not listed (staff, roles, practice settings, sign-ins, ...) are open to
# ``audit_log.read`` only. Entries the caller may not read are never returned, however they are
# reached.
RECORD_READ_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "client": ("client.read",),
    "client_contact": ("client.read",),
    "client_service": ("client.read",),
    "job": ("job.read",),
    "task": ("job.read",),
    "invoice": ("invoice.read",),
    "credit_note": ("invoice.read",),
    "estimate": ("estimate.read",),
    "recurring_schedule": ("recurring.read",),
    "payment": ("payment.read",),
    "disbursement": ("wip.read",),
    "wip_write_off": ("wip.read",),
    "time_entry": ("time.read_all",),
    "timesheet": ("time.read_all",),
    "expense_claim": ("expense.read_all",),
    "expense": ("expense.read_all",),
    "expense_receipt": ("expense.read_all",),
    "rate_rule": (COST_PERMISSION,),
    "staff_rate": (COST_PERMISSION,),
    "client_rate": (COST_PERMISSION,),
    "ticket": ("ticket.read",),
    "ticket_comment": ("ticket.read",),
    "ticket_attachment": ("ticket.read",),
    "period_lock": ("period_lock.read",),
}

# Record types that have related entries: those that name the record in their metadata. Only
# clients do: their contacts, services, rates, jobs, time, invoices and payments all carry
# ``client_id`` (and an expression index serves that lookup).
RELATED_KEYS: dict[str, str] = {"client": "client_id"}

# Keys whose values are cost rates, cost values or margins (``cost_rate``, ``cost_value_delta``,
# ``margin_bp`` ...). A rate rule of kind ``cost`` keeps its amount in ``rate``.
_COST_KEY = re.compile(r"(^|_)(cost|margin)(_|$)")


def label_of(action: str) -> str:
    """The action in words: its label, else the action itself made readable."""
    known = ACTION_LABELS.get(action)
    if known:
        return known
    words = action.replace(".", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def readable_types(permissions: frozenset[str]) -> frozenset[str]:
    """The record types whose history a caller with these permissions may read."""
    return frozenset(
        record_type
        for record_type, codes in RECORD_READ_PERMISSIONS.items()
        if any(code in permissions for code in codes)
    )


def redact_costs(value: Any, _hide_rate: bool = False) -> Any:
    """``value`` with every cost figure replaced by ``None`` (for people without ``rate.read``)."""
    if isinstance(value, dict):
        hide_rate = _hide_rate or value.get("kind") == "cost"
        return {
            key: None
            if _COST_KEY.search(str(key)) or (hide_rate and key == "rate")
            else redact_costs(item, hide_rate)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_costs(item, _hide_rate) for item in value]
    return value

"""Mounts the versioned API."""

from fastapi import APIRouter

from app.api.v1 import (
    audit,
    auth,
    central_clients,
    client_overview,
    clients,
    credit_note_wip,
    credit_notes,
    dashboard,
    dashboard_pages,
    data_import_export,
    debtors,
    disbursements,
    email_templates,
    estimates,
    expense_receipts,
    expense_types,
    expenses,
    grades,
    integration_routes,
    integrations,
    invoice_templates,
    invoices,
    job_budgets,
    job_calendar,
    job_tasks,
    jobs,
    members,
    notifications,
    onboarding,
    opening_balances,
    organizations,
    payments,
    people,
    period_locks,
    rate_rules,
    rates,
    recurring,
    reports,
    roles,
    segments,
    settings,
    staff,
    statement_sends,
    system,
    ticket_tags,
    tickets,
    time_entries,
    time_views,
    timesheet_reminders,
    timesheets,
    users,
    wip,
    wip_adjustments,
    wip_reports,
    wip_transfers,
    wip_views,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(audit.router)
api_router.include_router(organizations.me_router)
api_router.include_router(organizations.router)
api_router.include_router(members.router)
api_router.include_router(people.router)
# Before staff, rates and jobs: /staff/export, /rates/export and /jobs/export are not ids.
api_router.include_router(data_import_export.router)
api_router.include_router(staff.router)
api_router.include_router(grades.router)
api_router.include_router(roles.permissions_router)
api_router.include_router(roles.router)
api_router.include_router(settings.router)
api_router.include_router(segments.router)
api_router.include_router(period_locks.router)
api_router.include_router(onboarding.router)
api_router.include_router(rates.router)
api_router.include_router(rate_rules.router)
api_router.include_router(clients.router)
api_router.include_router(client_overview.router)
api_router.include_router(central_clients.router)
api_router.include_router(opening_balances.router)
api_router.include_router(integrations.router)
# After the sync-log route: its static paths come before the /integrations/{key} routes inside.
api_router.include_router(integration_routes.router)
api_router.include_router(invoice_templates.router)
# Before jobs: /jobs/budgets and /jobs/calendar must not be read as a /jobs/{job_id}.
api_router.include_router(job_budgets.router)
api_router.include_router(job_calendar.router)
api_router.include_router(jobs.router)
api_router.include_router(job_tasks.router)
# Before time_entries and timesheets: /time-entries/day and /timesheets/missing must not be
# read as an {entry_id} or {timesheet_id}.
api_router.include_router(time_views.router)
api_router.include_router(timesheet_reminders.router)
api_router.include_router(time_entries.router)
api_router.include_router(timesheets.router)
api_router.include_router(disbursements.router)
api_router.include_router(wip.router)
# The static /wip/* paths: summary, items, ledger, adjustments and transfers.
api_router.include_router(wip_views.router)
api_router.include_router(wip_adjustments.router)
api_router.include_router(wip_transfers.router)
api_router.include_router(invoices.router)
api_router.include_router(credit_notes.router)
api_router.include_router(credit_note_wip.router)
api_router.include_router(estimates.router)
api_router.include_router(recurring.router)
api_router.include_router(payments.router)
api_router.include_router(debtors.router)
api_router.include_router(statement_sends.router)
# Before reports: /reports/wip (and /reports/wip/items) are answered here.
api_router.include_router(wip_reports.router)
api_router.include_router(reports.router)
api_router.include_router(dashboard.router)
api_router.include_router(dashboard_pages.router)
api_router.include_router(notifications.router)
api_router.include_router(email_templates.router)
api_router.include_router(tickets.router)
api_router.include_router(ticket_tags.router)
api_router.include_router(expense_types.router)
api_router.include_router(expenses.router)
api_router.include_router(expense_receipts.router)

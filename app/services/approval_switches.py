"""The practice's approval switches (ticket APT53): each approval flow can be turned off.

One setting per area, all changed with ``PATCH /organizations/{id}`` (``organization.update``),
all on unless a practice switches them off (invoices: off unless switched on):

* ``require_timesheet_approval``: off, every timesheet is ``approval_exempt``; its time counts as
  approved and submitting the week approves it at once (``app/repositories/timesheets.py``).
* ``expense_approval_required``: off, a submitted expense claim is approved at once.
* ``wip_adjustment_approval_required``: off, every WIP adjustment takes effect at once.
* ``invoice_approval_required``: off, an invoice is issued without approval.

Switching an area off also settles what was waiting there, in the same transaction: each item is
approved as decided by the person who switched it off, with the note "Approval switched off" and
the usual audit entry and notifications of an approval. An item that its approval rules still
refuse is left waiting for a person to decide: an expense claim with a line dated in a locked
period, a WIP adjustment dated in a locked period or writing off more WIP than is now left.
Invoices are approved, not issued. Turning a switch back on changes nothing already decided; new
work needs approval again.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.repositories.approval_waiting import WaitingApprovalRepository
from app.services.expense_workflow import ExpenseWorkflowService
from app.services.invoices import InvoiceService
from app.services.policy import OrgContext
from app.services.timesheets import TimesheetService
from app.services.wip_adjustments import WipAdjustmentService

SETTLED_NOTE = "Approval switched off"

# The setting of each area, in the order the areas are settled.
SWITCHES: dict[str, str] = {
    "require_timesheet_approval": "timesheets",
    "expense_approval_required": "expense_claims",
    "wip_adjustment_approval_required": "wip_adjustments",
    "invoice_approval_required": "invoices",
}


async def settle_switched_off(
    session: AsyncSession,
    ctx: OrgContext,
    rctx: RequestContext,
    changes: dict[str, list[Any]],
) -> dict[str, dict[str, int]]:
    """Approve what is waiting in each area whose switch ``changes`` turns off.

    ``changes`` maps a setting to ``[old, new]``. Runs in the caller's unit of work, after the
    settings are written. Returns ``{area: {"approved": n, "left": m}}`` for those areas.
    """
    waiting = WaitingApprovalRepository(session)
    org_id = ctx.organization_id
    settled: dict[str, dict[str, int]] = {}
    for setting, area in SWITCHES.items():
        if changes.get(setting) != [True, False]:
            continue
        left = 0
        if area == "timesheets":
            approved = await TimesheetService(session).approve_waiting(
                ctx, await waiting.timesheets(org_id), SETTLED_NOTE, rctx
            )
        elif area == "expense_claims":
            approved, left = await ExpenseWorkflowService(session).approve_waiting(
                ctx, await waiting.expense_claims(org_id), SETTLED_NOTE, rctx
            )
        elif area == "wip_adjustments":
            approved, left = await WipAdjustmentService(session).approve_waiting(
                ctx, await waiting.wip_adjustments(org_id), SETTLED_NOTE, rctx
            )
        else:
            approved = await InvoiceService(session).approve_waiting(
                ctx, await waiting.invoices(org_id), SETTLED_NOTE, rctx
            )
        settled[area] = {"approved": approved, "left": left}
    return settled

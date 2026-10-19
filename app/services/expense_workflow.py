"""Submitting, recalling, approving and returning expense claims, and the approval queue
(docs/modules/expenses.md, "Workflow").

``draft -> submitted -> approved``, or ``submitted -> returned -> (edit) -> submitted``. Every
transition locks the claim row ``FOR UPDATE`` and leaves a row in the claim's history.

* Submit (the claimant, or anyone with ``expense.manage``): at least one line; every line above the
  practice's receipt threshold (gross) that is not mileage needs a receipt (422 ``RECEIPT_REQUIRED``
  naming the lines); every line date passes the period lock.
* Recall: the claimant pulls a ``submitted`` claim back to ``draft``.
* Approve and return need ``expense.approve`` as the claimant's manager or with
  ``expense.read_all``;
  nobody decides on their own claim unless they hold ``organization.update`` (403
  ``SELF_APPROVAL_FORBIDDEN``). Approving checks the period lock again; returning needs a reason
  (422 ``EXPENSE_RETURN_REASON_REQUIRED``).
* Approved, billable lines with a client and a recharge amount are work in progress.
"""

import uuid
from collections.abc import Sequence
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError
from app.db.models import Expense, ExpenseClaim, ExpenseClaimEvent
from app.db.session import unit_of_work
from app.repositories.expense_claims import ClaimFilters, ExpenseClaimRepository
from app.repositories.expense_lines import ExpenseLineRepository
from app.repositories.expense_receipts import ExpenseReceiptRepository
from app.schemas.expenses import ExpenseClaimDetail, ExpenseClaimRead
from app.services import audit, notification_events, period_locks, policy
from app.services.expense_access import EDITABLE_STATUSES, ExpenseAccess
from app.services.expense_reader import ExpenseReader
from app.services.expense_rules import (
    ClaimEmptyError,
    ReceiptRequiredError,
    ReturnReasonRequiredError,
    receipt_required,
)
from app.services.invoice_pdf import money
from app.services.policy import OrgContext
from app.services.time_rules import InvalidTransitionError, practice_now
from app.utils.pagination import Page, PageParams


class ExpenseWorkflowService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.claims = ExpenseClaimRepository(session)
        self.lines = ExpenseLineRepository(session)
        self.receipts = ExpenseReceiptRepository(session)
        self.access = ExpenseAccess(session)
        self.reader = ExpenseReader(session)

    # --- The queue -------------------------------------------------------------------------

    async def approvals(
        self,
        ctx: OrgContext,
        filters: ClaimFilters,
        *,
        params: PageParams,
    ) -> Page[ExpenseClaimRead]:
        """Submitted claims waiting for the caller, oldest submission first: the people the
        caller manages, or everyone's with ``expense.read_all``. The caller's own claim is
        listed only when they may approve it."""
        scope = await self.access.approval_scope(ctx)
        if scope is not None and not scope:
            return Page(items=[], next_cursor=None)
        own = None if policy.has_permission(ctx, "organization.update") else ctx.user.id
        claims, cursor = await self.claims.list_page(
            ctx.organization_id,
            replace(filters, statuses=("submitted",), scope_user_ids=scope, exclude_user_id=own),
            sort="submitted_at",
            descending=False,
            params=params,
        )
        return Page(items=await self.reader.claim_reads(ctx, claims), next_cursor=cursor)

    # --- Transitions -----------------------------------------------------------------------

    async def submit(
        self, ctx: OrgContext, claim_id: uuid.UUID, rctx: RequestContext
    ) -> ExpenseClaimDetail:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            claim = await self._lock(ctx, claim_id)
            ExpenseAccess.ensure_can_write(ctx, claim.user_id)
            self._ensure_status(claim, EDITABLE_STATUSES, "submitted", "submitted")
            lines = await self.lines.lock_lines_of(org_id, claim.id)
            if not lines:
                raise ClaimEmptyError(
                    details=[
                        {
                            "field": "lines",
                            "code": "empty",
                            "message": ClaimEmptyError.message,
                        }
                    ]
                )
            await period_locks.assert_open_many(
                self.session, org_id, {line.incurred_on for line in lines}, "expense"
            )
            await self._ensure_receipts(ctx, lines)
            old_status = claim.status
            submitted_at = practice_now()
            claim.status = "submitted"
            claim.submitted_at = submitted_at
            claim.decided_at = None
            claim.decided_by = None
            claim.decision_note = None
            await self._save(ctx, rctx, "expense_claim.submitted", claim, old_status, None)
            total = sum(line.gross for line in lines)
            names = await self.claims.names(org_id, [claim.user_id])
            await notification_events.expense_pending(
                self.session,
                organization_id=org_id,
                claim_id=claim.id,
                claimant_id=claim.user_id,
                claimant_name=names.get(claim.user_id, ""),
                claim_title=claim.title or "Expense claim",
                claim_total=money(total, ctx.organization.currency),
                submitted_at=submitted_at,
            )
            detail = await self.reader.claim_detail(ctx, claim)
        return detail

    async def recall(
        self, ctx: OrgContext, claim_id: uuid.UUID, rctx: RequestContext
    ) -> ExpenseClaimDetail:
        async with unit_of_work(self.session):
            claim = await self._lock(ctx, claim_id)
            ExpenseAccess.ensure_can_write(ctx, claim.user_id)
            self._ensure_status(claim, ("submitted",), "recalled", "draft")
            claim.status = "draft"
            claim.submitted_at = None
            await self._save(ctx, rctx, "expense_claim.recalled", claim, "submitted", None)
            detail = await self.reader.claim_detail(ctx, claim)
        return detail

    async def approve(
        self,
        ctx: OrgContext,
        claim_id: uuid.UUID,
        note: str | None,
        rctx: RequestContext,
    ) -> ExpenseClaimDetail:
        async with unit_of_work(self.session):
            claim = await self._lock(ctx, claim_id)
            await self._approve(ctx, claim, note, rctx)
            detail = await self.reader.claim_detail(ctx, claim)
        return detail

    async def bulk_approve(
        self, ctx: OrgContext, claim_ids: Sequence[uuid.UUID], rctx: RequestContext
    ) -> list[ExpenseClaimRead]:
        """All or nothing: every claim must exist, be submitted and be one the caller may
        approve."""
        wanted = list(dict.fromkeys(claim_ids))
        async with unit_of_work(self.session):
            claims = await self.claims.lock_many(ctx.organization_id, wanted)
            found = {claim.id for claim in claims}
            missing = [str(claim_id) for claim_id in wanted if claim_id not in found]
            if missing:
                raise NotFoundError("Expense claim not found.", details={"claim_ids": missing})
            for claim in claims:
                await self._approve(ctx, claim, None, rctx)
            by_id = {claim.id: claim for claim in claims}
            reads = await self.reader.claim_reads(ctx, [by_id[i] for i in wanted])
        return reads

    async def return_claim(
        self,
        ctx: OrgContext,
        claim_id: uuid.UUID,
        reason: str | None,
        rctx: RequestContext,
    ) -> ExpenseClaimDetail:
        note = (reason or "").strip()
        if not note:
            raise ReturnReasonRequiredError(
                details=[
                    {
                        "field": "reason",
                        "code": "required",
                        "message": ReturnReasonRequiredError.message,
                    }
                ]
            )
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            claim = await self._lock(ctx, claim_id)
            await self.access.ensure_can_decide(ctx, claim)
            self._ensure_status(claim, ("submitted",), "returned", "returned")
            decided_at = practice_now()
            claim.status = "returned"
            claim.decided_by = ctx.user.id
            claim.decided_at = decided_at
            claim.decision_note = note
            await self._save(ctx, rctx, "expense_claim.returned", claim, "submitted", note)
            if claim.user_id != ctx.user.id:
                total = (await self.claims.totals_for(org_id, [claim.id])).get(claim.id)
                await notification_events.expense_returned(
                    self.session,
                    organization_id=org_id,
                    claim_id=claim.id,
                    claimant_id=claim.user_id,
                    approver_name=ctx.user.full_name,
                    claim_title=claim.title or "Expense claim",
                    claim_total=money(total.gross if total else 0, ctx.organization.currency),
                    reason=note,
                    decided_at=decided_at,
                )
            detail = await self.reader.claim_detail(ctx, claim)
        return detail

    # --- Internals -------------------------------------------------------------------------

    async def _approve(
        self, ctx: OrgContext, claim: ExpenseClaim, note: str | None, rctx: RequestContext
    ) -> None:
        org_id = ctx.organization_id
        await self.access.ensure_can_decide(ctx, claim)
        self._ensure_status(claim, ("submitted",), "approved", "approved")
        lines = await self.lines.lines_of(org_id, claim.id)
        if lines:
            await period_locks.assert_open_many(
                self.session, org_id, {line.incurred_on for line in lines}, "expense"
            )
        decided_at = practice_now()
        claim.status = "approved"
        claim.decided_by = ctx.user.id
        claim.decided_at = decided_at
        claim.decision_note = note
        await self._save(ctx, rctx, "expense_claim.approved", claim, "submitted", note)
        if claim.user_id != ctx.user.id:
            await notification_events.expense_approved(
                self.session,
                organization_id=org_id,
                claim_id=claim.id,
                claimant_id=claim.user_id,
                approver_name=ctx.user.full_name,
                claim_title=claim.title or "Expense claim",
                claim_total=money(sum(line.gross for line in lines), ctx.organization.currency),
                decided_at=decided_at,
            )

    async def _lock(self, ctx: OrgContext, claim_id: uuid.UUID) -> ExpenseClaim:
        claim = await self.claims.get(ctx.organization_id, claim_id, lock=True)
        if claim is None:
            raise NotFoundError("Expense claim not found.")
        return claim

    @staticmethod
    def _ensure_status(
        claim: ExpenseClaim, allowed: Sequence[str], action: str, target: str
    ) -> None:
        if claim.status not in allowed:
            raise InvalidTransitionError(
                f"An expense claim that is {claim.status} cannot be {action}.",
                details={"from": claim.status, "to": target},
            )

    async def _ensure_receipts(self, ctx: OrgContext, lines: Sequence[Expense]) -> None:
        """422 ``RECEIPT_REQUIRED`` for the lines above the threshold that have no receipt."""
        threshold = ctx.organization.receipt_required_threshold
        needing = [
            line
            for line in lines
            if receipt_required(line.gross, threshold, is_mileage=line.miles is not None)
        ]
        if not needing:
            return
        held = await self.receipts.of_expenses(ctx.organization_id, [line.id for line in needing])
        missing = [line for line in needing if not held.get(line.id)]
        if missing:
            raise ReceiptRequiredError(
                details=[
                    {
                        "field": "lines",
                        "code": "receipt_required",
                        "message": "This line is above the receipt threshold: attach a receipt.",
                        "line_id": str(line.id),
                    }
                    for line in missing
                ]
            )

    async def _save(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        claim: ExpenseClaim,
        old_status: str,
        note: str | None,
    ) -> None:
        """Write the claim, its history row and its audit row."""
        org_id = ctx.organization_id
        claim = await self.claims.save(claim)
        kind = action.removeprefix("expense_claim.")
        await self.claims.add_event(
            ExpenseClaimEvent(
                organization_id=org_id,
                claim_id=claim.id,
                action=kind,
                actor_user_id=ctx.user.id,
                note=note,
            )
        )
        totals = (await self.claims.totals_for(org_id, [claim.id])).get(claim.id)
        names = await self.claims.names(org_id, [claim.user_id])
        meta: dict[str, object] = {
            "user_id": str(claim.user_id),
            "user_name": names.get(claim.user_id),
            "title": claim.title,
            "old_status": old_status,
            "new_status": claim.status,
            "line_count": totals.line_count if totals else 0,
            "gross": totals.gross if totals else 0,
        }
        if note:
            meta["note"] = note
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=org_id,
            target_type="expense_claim",
            target_id=claim.id,
            meta=meta,
        )

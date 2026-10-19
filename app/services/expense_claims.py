"""Expense claims and their lines (docs/modules/expenses.md, "Claims" and "Lines").

* A claim belongs to one person (the claimant, a member of the practice) and starts as a draft. It
  is written by the claimant with ``expense.log`` and by anyone with ``expense.manage``, while it
  is a ``draft`` or ``returned``; otherwise 409 ``EXPENSE_CLAIM_LOCKED``. Only a draft is deleted.
* Every write takes the claim row ``FOR UPDATE`` first, so a submission, an approval and an edit
  never interleave, and the number of lines and receipts is counted under the lock.
* An approved claim changes only through ``PATCH`` of a line by someone with ``expense.manage``,
  with a ``reason`` (422 ``EXPENSE_CORRECTION_REASON_REQUIRED``), an audit row with the changes
  and a ``corrected`` event on the claim; a billed line cannot (409 ``EXPENSE_BILLED``).
* Everything dated checks the period lock first (``period_locks.assert_open``, area ``expense``).
* The files of receipts are removed from the storage once the transaction that deleted their
  rows has committed.
"""

import uuid
from dataclasses import replace
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError, ValidationError
from app.db.models import Expense, ExpenseClaim, ExpenseClaimEvent
from app.db.session import unit_of_work
from app.repositories.expense_claims import ClaimFilters, ExpenseClaimRepository
from app.repositories.expense_lines import ExpenseLineRepository, LineFilters
from app.repositories.expense_receipts import ExpenseReceiptRepository
from app.schemas.expenses import (
    ApprovalBadge,
    ClaimStatusCounts,
    ExpenseClaimCreate,
    ExpenseClaimDetail,
    ExpenseClaimRead,
    ExpenseClaimUpdate,
    ExpenseLineCreate,
    ExpenseLineListItem,
    ExpenseLineRead,
    ExpenseLineUpdate,
    ExpenseSummary,
)
from app.services import period_locks, policy
from app.services.common import apply_changes, audit_excerpt, plain, record_audit, record_audit_many
from app.services.expense_access import (
    EDITABLE_STATUSES,
    ExpenseAccess,
)
from app.services.expense_lines import ExpenseLineBuilder
from app.services.expense_reader import ExpenseReader
from app.services.expense_rules import (
    MAX_LINES_PER_CLAIM,
    ClaimLineLimitError,
    CorrectionReasonRequiredError,
    ExpenseBilledError,
    ExpenseClaimLockedError,
)
from app.services.policy import OrgContext
from app.services.storage import FileStorage, discard
from app.utils.pagination import Page, PageParams


def _audit_value(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else plain(value)


def line_meta(claim: ExpenseClaim, line: Expense) -> dict[str, Any]:
    """What an audit row says about a line."""
    return {
        "claim_id": str(claim.id),
        "claimant_id": str(claim.user_id),
        "expense_type_id": str(line.expense_type_id),
        "description": audit_excerpt(line.description),
        "incurred_on": line.incurred_on.isoformat(),
        "client_id": str(line.client_id) if line.client_id else None,
        "net": line.net,
        "vat": line.vat,
        "gross": line.gross,
        "recharge_amount": line.recharge_amount,
        "is_billable": line.is_billable,
        "payer": line.payer,
    }


class ExpenseClaimService:
    def __init__(self, session: AsyncSession, storage: FileStorage) -> None:
        self.session = session
        self.storage = storage
        self.claims = ExpenseClaimRepository(session)
        self.lines = ExpenseLineRepository(session)
        self.receipts = ExpenseReceiptRepository(session)
        self.access = ExpenseAccess(session)
        self.builder = ExpenseLineBuilder(session)
        self.reader = ExpenseReader(session)

    # --- Reads -----------------------------------------------------------------------------

    async def list_claims(
        self,
        ctx: OrgContext,
        filters: ClaimFilters,
        *,
        mine: bool,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> Page[ExpenseClaimRead]:
        scope = await self.access.visible_user_ids(ctx)
        user_id = ctx.user.id if mine else filters.user_id
        if mine and filters.user_id not in (None, ctx.user.id):
            return Page(items=[], next_cursor=None)
        claims, cursor = await self.claims.list_page(
            ctx.organization_id,
            replace(filters, scope_user_ids=scope, user_id=user_id),
            sort=sort,
            descending=descending,
            params=params,
        )
        return Page(items=await self.reader.claim_reads(ctx, claims), next_cursor=cursor)

    async def get(self, ctx: OrgContext, claim_id: uuid.UUID) -> ExpenseClaimDetail:
        claim = await self.claims.get(ctx.organization_id, claim_id)
        if claim is None:
            raise NotFoundError("Expense claim not found.")
        await self.access.ensure_can_view(ctx, claim.user_id)
        return await self.reader.claim_detail(ctx, claim)

    async def summary(self, ctx: OrgContext) -> ExpenseSummary:
        org_id = ctx.organization_id
        scope = await self.access.visible_user_ids(ctx)
        found = await self.claims.status_counts(org_id, scope_user_ids=scope)

        def count(status: str) -> int:
            return found[status].count if status in found else 0

        waiting = ApprovalBadge(count=0, gross=0)
        if policy.has_permission(ctx, "expense.approve"):
            approval_scope = await self.access.approval_scope(ctx)
            mine = None if policy.has_permission(ctx, "organization.update") else ctx.user.id
            queue = await self.claims.status_counts(
                org_id,
                scope_user_ids=approval_scope,
                exclude_user_id=mine,
                statuses=("submitted",),
            )
            if "submitted" in queue:
                waiting = ApprovalBadge(
                    count=queue["submitted"].count, gross=queue["submitted"].gross
                )
        counts = ClaimStatusCounts(
            draft=count("draft"),
            submitted=count("submitted"),
            approved=count("approved"),
            returned=count("returned"),
            total=sum(item.count for item in found.values()),
        )
        pending = found["submitted"].gross if "submitted" in found else 0
        return ExpenseSummary(counts=counts, pending_gross=pending, awaiting_my_approval=waiting)

    async def list_lines(
        self,
        ctx: OrgContext,
        filters: LineFilters,
        *,
        mine: bool,
        needs_receipt: bool = False,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> Page[ExpenseLineListItem]:
        scope = await self.access.visible_user_ids(ctx)
        user_id = ctx.user.id if mine else filters.user_id
        if mine and filters.user_id not in (None, ctx.user.id):
            return Page(items=[], next_cursor=None)
        missing_over = ctx.organization.receipt_required_threshold if needs_receipt else None
        rows, cursor = await self.lines.list_page(
            ctx.organization_id,
            replace(
                filters,
                scope_user_ids=scope,
                user_id=user_id,
                missing_receipt_over=missing_over,
            ),
            sort=sort,
            descending=descending,
            params=params,
        )
        return Page(items=await self.reader.line_items(ctx, rows), next_cursor=cursor)

    # --- Claims ----------------------------------------------------------------------------

    async def create(
        self, ctx: OrgContext, data: ExpenseClaimCreate, rctx: RequestContext
    ) -> ExpenseClaimDetail:
        org_id = ctx.organization_id
        claimant_id = data.user_id or ctx.user.id
        ExpenseAccess.ensure_can_write(ctx, claimant_id)
        async with unit_of_work(self.session):
            await self._ensure_claimant(ctx, claimant_id)
            drafts = [await self.builder.assemble(ctx, line, None) for line in data.lines]
            if drafts:
                await period_locks.assert_open_many(
                    self.session, org_id, {d["incurred_on"] for d in drafts}, "expense"
                )
            claim = await self.claims.add(
                ExpenseClaim(
                    organization_id=org_id,
                    user_id=claimant_id,
                    created_by=ctx.user.id,
                    title=data.title,
                )
            )
            lines = [
                await self.lines.add(Expense(organization_id=org_id, claim_id=claim.id, **values))
                for values in drafts
            ]
            names = await self.claims.names(org_id, [claimant_id])
            await record_audit(
                self.session,
                ctx,
                rctx,
                "expense_claim.created",
                "expense_claim",
                claim.id,
                {
                    "user_id": str(claimant_id),
                    "user_name": names.get(claimant_id),
                    "title": claim.title,
                    "line_count": len(lines),
                    "gross": sum(line.gross for line in lines),
                },
            )
            await record_audit_many(
                self.session,
                ctx,
                rctx,
                [("expense.created", "expense", line.id, line_meta(claim, line)) for line in lines],
            )
            detail = await self.reader.claim_detail(ctx, claim)
        return detail

    async def update(
        self,
        ctx: OrgContext,
        claim_id: uuid.UUID,
        data: ExpenseClaimUpdate,
        rctx: RequestContext,
    ) -> ExpenseClaimDetail:
        async with unit_of_work(self.session):
            claim = await self._lock_for_edit(ctx, claim_id)
            changes = apply_changes(claim, data.model_dump(exclude_unset=True))
            claim = await self.claims.save(claim)
            if changes:
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "expense_claim.updated",
                    "expense_claim",
                    claim.id,
                    {"title": claim.title, "changes": changes},
                )
            detail = await self.reader.claim_detail(ctx, claim)
        return detail

    async def delete(self, ctx: OrgContext, claim_id: uuid.UUID, rctx: RequestContext) -> None:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            claim = await self.claims.get(org_id, claim_id, lock=True)
            if claim is None:
                raise NotFoundError("Expense claim not found.")
            ExpenseAccess.ensure_can_write(ctx, claim.user_id)
            if claim.status != "draft":
                raise ExpenseClaimLockedError(
                    "Only a draft claim can be deleted.", details={"status": claim.status}
                )
            lines = await self.lines.lines_of(org_id, claim.id)
            if lines:
                await period_locks.assert_open_many(
                    self.session, org_id, {line.incurred_on for line in lines}, "expense"
                )
            keys = await self.receipts.keys_of_claim(org_id, claim.id)
            meta = {
                "user_id": str(claim.user_id),
                "title": claim.title,
                "line_count": len(lines),
                "gross": sum(line.gross for line in lines),
            }
            claim_id = claim.id
            await self.claims.delete(claim)
            await record_audit(
                self.session, ctx, rctx, "expense_claim.deleted", "expense_claim", claim_id, meta
            )
        await discard(self.storage, org_id, keys)

    # --- Lines -----------------------------------------------------------------------------

    async def add_line(
        self,
        ctx: OrgContext,
        claim_id: uuid.UUID,
        data: ExpenseLineCreate,
        rctx: RequestContext,
    ) -> ExpenseLineRead:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            claim = await self._lock_for_edit(ctx, claim_id)
            if await self.lines.count_in_claim(org_id, claim.id) >= MAX_LINES_PER_CLAIM:
                raise ClaimLineLimitError(details={"max_lines": MAX_LINES_PER_CLAIM})
            values = await self.builder.assemble(ctx, data, None)
            await period_locks.assert_open(self.session, org_id, values["incurred_on"], "expense")
            line = await self.lines.add(
                Expense(organization_id=org_id, claim_id=claim.id, **values)
            )
            await record_audit(
                self.session,
                ctx,
                rctx,
                "expense.created",
                "expense",
                line.id,
                line_meta(claim, line),
            )
            [read] = await self.reader.line_reads(ctx, [line])
        return read

    async def update_line(
        self,
        ctx: OrgContext,
        claim_id: uuid.UUID,
        line_id: uuid.UUID,
        data: ExpenseLineUpdate,
        rctx: RequestContext,
    ) -> ExpenseLineRead:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            claim = await self.claims.get(org_id, claim_id, lock=True)
            if claim is None:
                raise NotFoundError("Expense claim not found.")
            ExpenseAccess.ensure_can_write(ctx, claim.user_id)
            line = await self.lines.get(org_id, line_id, lock=True)
            if line is None or line.claim_id != claim.id:
                raise NotFoundError("Expense not found.")
            correcting = claim.status == "approved" and policy.has_permission(ctx, "expense.manage")
            if claim.status not in EDITABLE_STATUSES and not correcting:
                raise ExpenseClaimLockedError(
                    f"This claim is {claim.status} and cannot be changed.",
                    details={"status": claim.status},
                )
            if correcting:
                if line.billing_status != "unbilled":
                    raise ExpenseBilledError(details={"billing_status": line.billing_status})
                if not data.reason:
                    raise CorrectionReasonRequiredError(
                        details=[
                            {
                                "field": "reason",
                                "code": "required",
                                "message": CorrectionReasonRequiredError.message,
                            }
                        ]
                    )
            values = await self.builder.assemble(ctx, data, line)
            changes = {
                name: [_audit_value(getattr(line, name)), _audit_value(new)]
                for name, new in values.items()
                if getattr(line, name) != new
            }
            if changes:
                await period_locks.assert_open_many(
                    self.session, org_id, {line.incurred_on, values["incurred_on"]}, "expense"
                )
                for name, new in values.items():
                    setattr(line, name, new)
                line = await self.lines.save(line)
                meta = {**line_meta(claim, line), "changes": changes}
                if data.reason:
                    meta["reason"] = data.reason
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "expense.corrected" if correcting else "expense.updated",
                    "expense",
                    line.id,
                    meta,
                )
                if correcting:
                    await self.claims.add_event(
                        ExpenseClaimEvent(
                            organization_id=org_id,
                            claim_id=claim.id,
                            action="corrected",
                            actor_user_id=ctx.user.id,
                            note=data.reason,
                        )
                    )
            [read] = await self.reader.line_reads(ctx, [line])
        return read

    async def delete_line(
        self,
        ctx: OrgContext,
        claim_id: uuid.UUID,
        line_id: uuid.UUID,
        rctx: RequestContext,
    ) -> None:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            claim = await self._lock_for_edit(ctx, claim_id)
            line = await self.lines.get(org_id, line_id, lock=True)
            if line is None or line.claim_id != claim.id:
                raise NotFoundError("Expense not found.")
            await period_locks.assert_open(self.session, org_id, line.incurred_on, "expense")
            keys = await self.receipts.keys_of_expense(org_id, line.id)
            meta = line_meta(claim, line)
            line_id = line.id
            await self.lines.delete(line)
            await record_audit(self.session, ctx, rctx, "expense.deleted", "expense", line_id, meta)
        await discard(self.storage, org_id, keys)

    # --- Internals -------------------------------------------------------------------------

    async def _lock_for_edit(self, ctx: OrgContext, claim_id: uuid.UUID) -> ExpenseClaim:
        """The claim, locked, when the caller may write it and it is a draft or returned."""
        claim = await self.claims.get(ctx.organization_id, claim_id, lock=True)
        if claim is None:
            raise NotFoundError("Expense claim not found.")
        ExpenseAccess.ensure_can_write(ctx, claim.user_id)
        if claim.status not in EDITABLE_STATUSES:
            raise ExpenseClaimLockedError(
                f"This claim is {claim.status} and cannot be changed.",
                details={"status": claim.status},
            )
        return claim

    async def _ensure_claimant(self, ctx: OrgContext, claimant_id: uuid.UUID) -> None:
        if claimant_id == ctx.user.id:
            return
        member = await self.access.refs.get_member(ctx.organization_id, claimant_id)
        if member is None:
            raise NotFoundError("Staff member not found.", details={"field": "user_id"})
        if member.status != "active":
            raise ValidationError.for_field(
                "user_id", "Claims can only be recorded for active staff.", "inactive"
            )

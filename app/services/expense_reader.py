"""Building the response models of expense claims, lines and receipts.

Every method loads what it needs for the whole page at once (types, clients, jobs, receipts,
names, managers): the number of queries does not depend on the number of rows.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, Expense, ExpenseClaim, ExpenseReceipt, ExpenseType, Job
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.expense_claims import ClaimTotalsRow, ExpenseClaimRepository
from app.repositories.expense_lines import ExpenseLineRepository
from app.repositories.expense_receipts import ExpenseReceiptRepository
from app.repositories.expense_types import ExpenseTypeRepository
from app.schemas.billing import ClientRef, JobRef
from app.schemas.expenses import (
    ClaimEventRead,
    ClaimTotals,
    ExpenseClaimDetail,
    ExpenseClaimRead,
    ExpenseLineListItem,
    ExpenseLineRead,
    ReceiptListItem,
    ReceiptRead,
    TypeRef,
)
from app.services.expense_access import claim_actions
from app.services.expense_rules import receipt_required
from app.services.policy import OrgContext


@dataclass(slots=True)
class _Lookups:
    types: dict[uuid.UUID, ExpenseType]
    clients: dict[uuid.UUID, Client]
    jobs: dict[uuid.UUID, Job]
    receipts: dict[uuid.UUID, list[ExpenseReceipt]]
    names: dict[uuid.UUID, str]


def receipt_read(receipt: ExpenseReceipt, names: dict[uuid.UUID, str]) -> ReceiptRead:
    return ReceiptRead(
        id=receipt.id,
        expense_id=receipt.expense_id,
        file_name=receipt.file_name,
        content_type=receipt.content_type,
        size_bytes=receipt.size_bytes,
        is_image=receipt.content_type.startswith("image/"),
        uploaded_by=receipt.uploaded_by,
        uploaded_by_name=names.get(receipt.uploaded_by),
        created_at=receipt.created_at,
    )


class ExpenseReader:
    def __init__(self, session: AsyncSession) -> None:
        self.claims = ExpenseClaimRepository(session)
        self.lines = ExpenseLineRepository(session)
        self.receipts = ExpenseReceiptRepository(session)
        self.types = ExpenseTypeRepository(session)
        self.refs = BillingReferenceRepository(session)

    # --- Lines -----------------------------------------------------------------------------

    async def _lookups(self, ctx: OrgContext, lines: Collection[Expense]) -> _Lookups:
        org_id = ctx.organization_id
        receipts = await self.receipts.of_expenses(org_id, {line.id for line in lines})
        uploaders = [r.uploaded_by for group in receipts.values() for r in group]
        return _Lookups(
            types=await self.types.by_ids(org_id, {line.expense_type_id for line in lines}),
            clients=await self.refs.clients_by_ids(
                org_id, {line.client_id for line in lines if line.client_id is not None}
            ),
            jobs=await self.refs.jobs_by_ids(org_id, {line.job_id for line in lines}),
            receipts=receipts,
            names=await self.claims.names(org_id, uploaders),
        )

    @staticmethod
    def _line_fields(ctx: OrgContext, line: Expense, look: _Lookups) -> dict[str, Any]:
        expense_type = look.types[line.expense_type_id]
        client = look.clients.get(line.client_id) if line.client_id else None
        job = look.jobs.get(line.job_id) if line.job_id else None
        receipts = look.receipts.get(line.id, [])
        threshold = ctx.organization.receipt_required_threshold
        return {
            "id": line.id,
            "claim_id": line.claim_id,
            "expense_type_id": line.expense_type_id,
            "expense_type": TypeRef.from_model(expense_type),
            "incurred_on": line.incurred_on,
            "description": line.description,
            "client_id": line.client_id,
            "client": ClientRef.from_model(client) if client else None,
            "job_id": line.job_id,
            "job": JobRef.from_model(job) if job else None,
            "supplier": line.supplier,
            "vat_rate_id": line.vat_rate_id,
            "vat_rate_bp": line.vat_rate_bp,
            "net": line.net,
            "vat": line.vat,
            "gross": line.gross,
            "miles": line.miles,
            "mileage_rate_applied": line.mileage_rate_applied,
            "is_billable": line.is_billable,
            "markup_bp": line.markup_bp,
            "recharge_amount": line.recharge_amount,
            "payer": line.payer,
            "billing_status": line.billing_status,
            "receipt_required": receipt_required(
                line.gross, threshold, is_mileage=line.miles is not None
            ),
            "receipt_count": len(receipts),
            "receipts": [receipt_read(r, look.names) for r in receipts],
            "created_at": line.created_at,
            "updated_at": line.updated_at,
        }

    async def line_reads(self, ctx: OrgContext, lines: Sequence[Expense]) -> list[ExpenseLineRead]:
        look = await self._lookups(ctx, lines)
        return [ExpenseLineRead(**self._line_fields(ctx, line, look)) for line in lines]

    async def line_items(
        self, ctx: OrgContext, rows: Sequence[tuple[Expense, ExpenseClaim]]
    ) -> list[ExpenseLineListItem]:
        look = await self._lookups(ctx, [line for line, _ in rows])
        names = await self.claims.names(ctx.organization_id, {claim.user_id for _, claim in rows})
        return [
            ExpenseLineListItem(
                **self._line_fields(ctx, line, look),
                claim_status=claim.status,
                claim_title=claim.title,
                user_id=claim.user_id,
                user_name=names.get(claim.user_id),
            )
            for line, claim in rows
        ]

    # --- Receipts --------------------------------------------------------------------------

    async def receipt_one(self, ctx: OrgContext, receipt: ExpenseReceipt) -> ReceiptRead:
        names = await self.claims.names(ctx.organization_id, [receipt.uploaded_by])
        return receipt_read(receipt, names)

    async def receipt_items(
        self, ctx: OrgContext, rows: Sequence[tuple[ExpenseReceipt, Expense, ExpenseClaim]]
    ) -> list[ReceiptListItem]:
        org_id = ctx.organization_id
        names = await self.claims.names(
            org_id, {receipt.uploaded_by for receipt, _, _ in rows} | {c.user_id for *_, c in rows}
        )
        clients = await self.refs.clients_by_ids(
            org_id, {line.client_id for _, line, _ in rows if line.client_id is not None}
        )
        items = []
        for receipt, line, claim in rows:
            client = clients.get(line.client_id) if line.client_id else None
            items.append(
                ReceiptListItem(
                    **receipt_read(receipt, names).model_dump(),
                    claim_id=claim.id,
                    claim_status=claim.status,
                    claim_title=claim.title,
                    user_id=claim.user_id,
                    user_name=names.get(claim.user_id),
                    description=line.description,
                    incurred_on=line.incurred_on,
                    gross=line.gross,
                    client=ClientRef.from_model(client) if client else None,
                )
            )
        return items

    # --- Claims ----------------------------------------------------------------------------

    async def claim_reads(
        self, ctx: OrgContext, claims: Sequence[ExpenseClaim]
    ) -> list[ExpenseClaimRead]:
        org_id = ctx.organization_id
        totals = await self.claims.totals_for(org_id, [c.id for c in claims])
        names = await self.claims.names(
            org_id, [uid for c in claims for uid in (c.user_id, c.created_by, c.decided_by)]
        )
        managers = await self.claims.managers_of(org_id, {c.user_id for c in claims})
        return [
            self._claim_read(ctx, c, totals.get(c.id, ClaimTotalsRow()), names, managers)
            for c in claims
        ]

    @staticmethod
    def _claim_read(
        ctx: OrgContext,
        claim: ExpenseClaim,
        totals: ClaimTotalsRow,
        names: dict[uuid.UUID, str],
        managers: dict[uuid.UUID, uuid.UUID | None],
    ) -> ExpenseClaimRead:
        return ExpenseClaimRead(
            id=claim.id,
            user_id=claim.user_id,
            user_name=names.get(claim.user_id),
            created_by=claim.created_by,
            created_by_name=names.get(claim.created_by),
            title=claim.title,
            status=claim.status,
            submitted_at=claim.submitted_at,
            decided_at=claim.decided_at,
            decided_by=claim.decided_by,
            decided_by_name=names.get(claim.decided_by) if claim.decided_by else None,
            decision_note=claim.decision_note,
            line_count=totals.line_count,
            first_date=totals.first_date,
            last_date=totals.last_date,
            totals=ClaimTotals(
                net=totals.net,
                vat=totals.vat,
                gross=totals.gross,
                recharge=totals.recharge,
                reimbursable=totals.reimbursable,
            ),
            actions=claim_actions(ctx, claim, managers.get(claim.user_id)),
            created_at=claim.created_at,
            updated_at=claim.updated_at,
        )

    async def claim_detail(self, ctx: OrgContext, claim: ExpenseClaim) -> ExpenseClaimDetail:
        org_id = ctx.organization_id
        [base] = await self.claim_reads(ctx, [claim])
        lines = await self.lines.lines_of(org_id, claim.id)
        line_reads = await self.line_reads(ctx, lines)
        events = await self.claims.events_of(org_id, claim.id)
        names = await self.claims.names(org_id, [e.actor_user_id for e in events])
        return ExpenseClaimDetail(
            **base.model_dump(),
            lines=line_reads,
            events=[
                ClaimEventRead(
                    id=e.id,
                    action=e.action,
                    actor_user_id=e.actor_user_id,
                    actor_name=names.get(e.actor_user_id),
                    note=e.note,
                    created_at=e.created_at,
                )
                for e in events
            ],
        )

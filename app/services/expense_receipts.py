"""Expense receipts (docs/modules/expenses.md, "Receipts").

* An upload is checked first (size, extension, content, declared type: see ``receipt_files``) and
  only then touches the file storage and the database. The file is written inside the transaction
  that records it and removed again when that transaction fails; deleting a receipt (or its line
  or claim) removes the file once the transaction has committed. Files are stored under a random key
  in the practice's own area.
* A line holds at most 5 receipts (409 ``RECEIPT_LIMIT_REACHED``), counted under the claim's row
  lock, so two uploads cannot both take the last place. Receipts are added and deleted by whoever
  may write the claim, while it is a ``draft`` or ``returned`` (409 ``EXPENSE_CLAIM_LOCKED``).
* Whoever may see the claim may list and download its receipts.
* A store that cannot take the file answers 503 ``STORAGE_UNAVAILABLE``; one that has lost a file
  answers 404 ``RECEIPT_FILE_MISSING`` on download.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError, ServiceUnavailableError
from app.db.models import ExpenseClaim, ExpenseReceipt
from app.db.models.expenses import MAX_RECEIPTS_PER_LINE
from app.db.session import unit_of_work
from app.repositories.expense_claims import ExpenseClaimRepository
from app.repositories.expense_lines import ExpenseLineRepository
from app.repositories.expense_receipts import ExpenseReceiptRepository, ReceiptFilters
from app.schemas.expenses import ReceiptListItem, ReceiptRead
from app.services.common import record_audit
from app.services.expense_access import EDITABLE_STATUSES, ExpenseAccess
from app.services.expense_reader import ExpenseReader
from app.services.expense_rules import ExpenseClaimLockedError, ReceiptLimitError
from app.services.policy import OrgContext
from app.services.receipt_files import inspect_receipt
from app.services.storage import FileStorage, StorageError, discard, new_key
from app.utils.pagination import Page, PageParams


class ExpenseReceiptService:
    def __init__(self, session: AsyncSession, storage: FileStorage) -> None:
        self.session = session
        self.storage = storage
        self.claims = ExpenseClaimRepository(session)
        self.lines = ExpenseLineRepository(session)
        self.receipts = ExpenseReceiptRepository(session)
        self.access = ExpenseAccess(session)
        self.reader = ExpenseReader(session)

    # --- Reads -----------------------------------------------------------------------------

    async def list_receipts(
        self,
        ctx: OrgContext,
        filters: ReceiptFilters,
        *,
        mine: bool,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> Page[ReceiptListItem]:
        scope = await self.access.visible_user_ids(ctx)
        user_id = ctx.user.id if mine else filters.user_id
        if mine and filters.user_id not in (None, ctx.user.id):
            return Page(items=[], next_cursor=None)
        rows, cursor = await self.receipts.list_page(
            ctx.organization_id,
            replace(filters, scope_user_ids=scope, user_id=user_id),
            sort=sort,
            descending=descending,
            params=params,
        )
        return Page(items=await self.reader.receipt_items(ctx, rows), next_cursor=cursor)

    async def get(
        self, ctx: OrgContext, line_id: uuid.UUID, receipt_id: uuid.UUID
    ) -> ExpenseReceipt:
        """The receipt, for whoever may see its claim."""
        org_id = ctx.organization_id
        line = await self.lines.get(org_id, line_id)
        claim = await self.claims.get(org_id, line.claim_id) if line else None
        if claim is None:
            raise NotFoundError("Expense not found.")
        await self.access.ensure_can_view(ctx, claim.user_id)
        receipt = await self.receipts.get(org_id, line_id, receipt_id)
        if receipt is None:
            raise NotFoundError("Receipt not found.")
        return receipt

    async def open(self, receipt: ExpenseReceipt) -> AsyncIterator[bytes]:
        """The file's content. 404 ``RECEIPT_FILE_MISSING`` when the store lost the file."""
        try:
            return await self.storage.open(receipt.organization_id, receipt.storage_key)
        except StorageError as exc:
            raise NotFoundError(
                "The file is no longer available.", code="RECEIPT_FILE_MISSING"
            ) from exc

    # --- Writes ----------------------------------------------------------------------------

    async def ensure_can_attach(self, ctx: OrgContext, line_id: uuid.UUID) -> None:
        """404 / 403 / 409 for an upload that cannot succeed: asked before its body is read."""
        org_id = ctx.organization_id
        line = await self.lines.get(org_id, line_id)
        claim = await self.claims.get(org_id, line.claim_id) if line else None
        if claim is None:
            raise NotFoundError("Expense not found.")
        self._ensure_writable(ctx, claim)

    async def add(
        self,
        ctx: OrgContext,
        line_id: uuid.UUID,
        *,
        file_name: str,
        declared_type: str | None,
        data: bytes,
        rctx: RequestContext,
    ) -> ReceiptRead:
        inspected = inspect_receipt(file_name, declared_type, data)
        org_id = ctx.organization_id
        key = new_key()
        written = False
        try:
            async with unit_of_work(self.session):
                claim = await self._lock_claim_of(org_id, line_id)
                self._ensure_writable(ctx, claim)
                line = await self.lines.get(org_id, line_id, lock=True)
                if line is None:
                    raise NotFoundError("Expense not found.")
                if await self.receipts.count_of(org_id, line.id) >= MAX_RECEIPTS_PER_LINE:
                    raise ReceiptLimitError(details={"max_receipts": MAX_RECEIPTS_PER_LINE})
                # From here a failure must remove the file again (``discard`` below).
                written = True
                try:
                    await self.storage.put(org_id, key, data)
                except StorageError as exc:
                    raise ServiceUnavailableError(
                        "File storage is unavailable. Please try again shortly.",
                        code="STORAGE_UNAVAILABLE",
                    ) from exc
                receipt = await self.receipts.add(
                    ExpenseReceipt(
                        organization_id=org_id,
                        expense_id=line.id,
                        file_name=inspected.file_name,
                        content_type=inspected.content_type,
                        size_bytes=inspected.size_bytes,
                        storage_key=key,
                        sha256=inspected.sha256,
                        uploaded_by=ctx.user.id,
                    )
                )
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "expense_receipt.added",
                    "expense_receipt",
                    receipt.id,
                    {
                        "claim_id": str(claim.id),
                        "expense_id": str(line.id),
                        "file_name": receipt.file_name,
                        "content_type": receipt.content_type,
                        "size_bytes": receipt.size_bytes,
                    },
                )
                result = await self.reader.receipt_one(ctx, receipt)
        except BaseException:
            if written:
                await discard(self.storage, org_id, [key])
            raise
        return result

    async def delete(
        self,
        ctx: OrgContext,
        line_id: uuid.UUID,
        receipt_id: uuid.UUID,
        rctx: RequestContext,
    ) -> None:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            claim = await self._lock_claim_of(org_id, line_id)
            self._ensure_writable(ctx, claim)
            receipt = await self.receipts.get(org_id, line_id, receipt_id, lock=True)
            if receipt is None:
                raise NotFoundError("Receipt not found.")
            key = receipt.storage_key
            meta = {
                "claim_id": str(claim.id),
                "expense_id": str(line_id),
                "file_name": receipt.file_name,
                "size_bytes": receipt.size_bytes,
            }
            await self.receipts.delete(receipt)
            await record_audit(
                self.session,
                ctx,
                rctx,
                "expense_receipt.deleted",
                "expense_receipt",
                receipt_id,
                meta,
            )
        await discard(self.storage, org_id, [key])

    # --- Internals -------------------------------------------------------------------------

    async def _lock_claim_of(self, org_id: uuid.UUID, line_id: uuid.UUID) -> ExpenseClaim:
        line = await self.lines.get(org_id, line_id)
        claim = await self.claims.get(org_id, line.claim_id, lock=True) if line else None
        if claim is None:
            raise NotFoundError("Expense not found.")
        return claim

    @staticmethod
    def _ensure_writable(ctx: OrgContext, claim: ExpenseClaim) -> None:
        ExpenseAccess.ensure_can_write(ctx, claim.user_id)
        if claim.status not in EDITABLE_STATUSES:
            raise ExpenseClaimLockedError(
                f"This claim is {claim.status}: its receipts cannot be changed.",
                details={"status": claim.status},
            )

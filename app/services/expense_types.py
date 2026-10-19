"""Expense types (docs/modules/expenses.md, "Expense types").

A type says what a claim line is for: it may price a line from miles (``is_mileage``), start it
with a VAT code and make it billable. The name is unique among the practice's active types and the
code among all of them. A type that expense lines use is deactivated, never deleted (409
``EXPENSE_TYPE_IN_USE``), and cannot start or stop being a mileage type. Reading needs
``expense.log``, ``expense.read_all`` or ``expense.manage``; writing ``expense.manage``.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError
from app.db.models import ExpenseType
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.expense_types import ExpenseTypeRepository, TypeFilters
from app.schemas.expenses import ExpenseTypeCreate, ExpenseTypeRead, ExpenseTypeUpdate
from app.services import policy
from app.services.billing_common import VatResolver
from app.services.common import apply_changes, duplicate_error, record_audit
from app.services.expense_rules import ExpenseTypeInUseError
from app.services.policy import OrgContext
from app.utils.pagination import Page, PageParams

READ_CODES = ("expense.log", "expense.read_all", "expense.manage")


class ExpenseTypeService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.types = ExpenseTypeRepository(session)
        self.refs = BillingReferenceRepository(session)

    async def _reads(self, ctx: OrgContext, rows: list[ExpenseType]) -> list[ExpenseTypeRead]:
        used = await self.types.used_ids(ctx.organization_id, [t.id for t in rows])
        return [ExpenseTypeRead.from_model(t, is_used=t.id in used) for t in rows]

    # --- Reads ----------------------------------------------------------------------------

    async def list_types(
        self,
        ctx: OrgContext,
        filters: TypeFilters,
        *,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> Page[ExpenseTypeRead]:
        policy.ensure_any(ctx, *READ_CODES)
        page = await self.types.list_page(
            ctx.organization_id, filters, sort=sort, descending=descending, params=params
        )
        return Page(items=await self._reads(ctx, page.items), next_cursor=page.next_cursor)

    async def get(self, ctx: OrgContext, type_id: uuid.UUID) -> ExpenseTypeRead:
        policy.ensure_any(ctx, *READ_CODES)
        row = await self.types.get(ctx.organization_id, type_id)
        if row is None:
            raise NotFoundError("Expense type not found.")
        [read] = await self._reads(ctx, [row])
        return read

    # --- Writes ---------------------------------------------------------------------------

    async def create(
        self, ctx: OrgContext, data: ExpenseTypeCreate, rctx: RequestContext
    ) -> ExpenseTypeRead:
        policy.ensure(ctx, "expense.manage")
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            await self._ensure_unique(
                org_id,
                name=data.name if data.is_active else None,
                code=data.code,
                exclude_id=None,
            )
            if data.default_vat_rate_id is not None:
                await VatResolver(self.refs, ctx.organization).chosen(
                    data.default_vat_rate_id, "default_vat_rate_id"
                )
            row = await self.types.add(ExpenseType(organization_id=org_id, **data.model_dump()))
            await record_audit(
                self.session,
                ctx,
                rctx,
                "expense_type.created",
                "expense_type",
                row.id,
                {"name": row.name, "code": row.code, "is_mileage": row.is_mileage},
            )
            [read] = await self._reads(ctx, [row])
        return read

    async def update(
        self,
        ctx: OrgContext,
        type_id: uuid.UUID,
        data: ExpenseTypeUpdate,
        rctx: RequestContext,
    ) -> ExpenseTypeRead:
        policy.ensure(ctx, "expense.manage")
        org_id = ctx.organization_id
        values = data.model_dump(exclude_unset=True)
        async with unit_of_work(self.session):
            row = await self.types.get(org_id, type_id, lock=True)
            if row is None:
                raise NotFoundError("Expense type not found.")
            active = values.get("is_active", row.is_active)
            # A rename, or bringing an inactive type back, claims the name again.
            claims_name = active and ("name" in values or not row.is_active)
            await self._ensure_unique(
                org_id,
                name=values.get("name", row.name) if claims_name else None,
                code=values.get("code"),
                exclude_id=row.id,
            )
            if (
                "is_mileage" in values
                and values["is_mileage"] != row.is_mileage
                and await self.types.is_used(org_id, row.id)
            ):
                raise ExpenseTypeInUseError(
                    "Expenses use this type: it cannot become or stop being a mileage type."
                )
            if values.get("default_vat_rate_id") not in (None, row.default_vat_rate_id):
                await VatResolver(self.refs, ctx.organization).chosen(
                    values["default_vat_rate_id"], "default_vat_rate_id"
                )
            changes = apply_changes(row, values)
            row = await self.types.save(row)
            if changes:
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "expense_type.updated",
                    "expense_type",
                    row.id,
                    {"name": row.name, "code": row.code, "changes": changes},
                )
            [read] = await self._reads(ctx, [row])
        return read

    async def delete(self, ctx: OrgContext, type_id: uuid.UUID, rctx: RequestContext) -> None:
        policy.ensure(ctx, "expense.manage")
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            row = await self.types.get(org_id, type_id, lock=True)
            if row is None:
                raise NotFoundError("Expense type not found.")
            if await self.types.is_used(org_id, row.id):
                raise ExpenseTypeInUseError(
                    details={"expense_type_id": str(row.id), "is_active": row.is_active}
                )
            meta = {"name": row.name, "code": row.code}
            await self.types.delete(org_id, row.id)
            await record_audit(
                self.session, ctx, rctx, "expense_type.deleted", "expense_type", row.id, meta
            )

    # --- Internals ------------------------------------------------------------------------

    async def _ensure_unique(
        self,
        org_id: uuid.UUID,
        *,
        name: str | None,
        code: str | None,
        exclude_id: uuid.UUID | None,
    ) -> None:
        """409 when ``code`` is used by any type, or ``name`` by another active type."""
        if code is not None and await self.types.code_taken(org_id, code, exclude_id=exclude_id):
            raise duplicate_error(
                "code", "An expense type with this code already exists.", "EXPENSE_TYPE_CODE_TAKEN"
            )
        if name is not None and await self.types.name_taken(org_id, name, exclude_id=exclude_id):
            raise duplicate_error(
                "name",
                "An active expense type with this name already exists.",
                "EXPENSE_TYPE_NAME_TAKEN",
            )

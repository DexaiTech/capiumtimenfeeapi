"""Disbursements: costs paid on a client's behalf, recharged on an invoice.

Anyone with ``disbursement.log`` records and edits their own; ``disbursement.manage`` edits
anyone's. Reading needs ``wip.read`` except for one's own. Only ``unbilled``
disbursements can be changed or deleted.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.db.models import Client, Disbursement
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.disbursements import DisbursementFilters, DisbursementRepository
from app.schemas.billing import DisbursementCreate, DisbursementUpdate
from app.services import period_locks, policy
from app.services.billing_common import (
    VatResolver,
    conflict,
    get_client_or_404,
    record,
)
from app.services.clients import ensure_client_accepts_work
from app.services.policy import OrgContext
from app.utils.pagination import Page, PageParams

_AUDITED_FIELDS = (
    "client_id",
    "job_id",
    "incurred_on",
    "description",
    "supplier",
    "amount",
    "vat_rate_id",
    "is_billable",
)


def _plain(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


class DisbursementService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.disbursements = DisbursementRepository(session)
        self.refs = BillingReferenceRepository(session)

    @staticmethod
    def _can_write(ctx: OrgContext, row: Disbursement) -> bool:
        if policy.has_permission(ctx, "disbursement.manage"):
            return True
        return policy.has_permission(ctx, "disbursement.log") and row.created_by == ctx.user.id

    async def list_disbursements(
        self, ctx: OrgContext, filters: DisbursementFilters, params: PageParams
    ) -> Page[Disbursement]:
        if not policy.has_permission(ctx, "wip.read"):
            if not (
                policy.has_permission(ctx, "disbursement.log")
                or policy.has_permission(ctx, "disbursement.manage")
            ):
                raise AuthorizationError()
            # Without wip.read a person sees only what they recorded.
            filters = DisbursementFilters(
                client_id=filters.client_id,
                job_id=filters.job_id,
                billing_status=filters.billing_status,
                created_by=ctx.user.id,
                date_from=filters.date_from,
                date_to=filters.date_to,
                is_billable=filters.is_billable,
            )
        return await self.disbursements.list_page(ctx.organization_id, filters, params)

    async def get(self, ctx: OrgContext, disbursement_id: uuid.UUID) -> Disbursement:
        row = await self.disbursements.get(ctx.organization_id, disbursement_id)
        if row is None:
            raise NotFoundError("Disbursement not found.")
        if not policy.has_permission(ctx, "wip.read") and row.created_by != ctx.user.id:
            raise AuthorizationError()
        return row

    async def create(
        self, ctx: OrgContext, data: DisbursementCreate, rctx: RequestContext
    ) -> Disbursement:
        policy_ok = policy.has_permission(ctx, "disbursement.log") or policy.has_permission(
            ctx, "disbursement.manage"
        )
        if not policy_ok:
            raise AuthorizationError()
        async with unit_of_work(self.session):
            client = await ensure_client_accepts_work(
                self.session, ctx.organization_id, data.client_id
            )
            await self._check_job(ctx, client, data.job_id)
            if data.vat_rate_id is not None:
                await VatResolver(self.refs, ctx.organization).chosen(data.vat_rate_id)
            await period_locks.assert_open(
                self.session, ctx.organization_id, data.incurred_on, "expense"
            )
            row = await self.disbursements.add(
                Disbursement(
                    organization_id=ctx.organization_id,
                    client_id=client.id,
                    job_id=data.job_id,
                    incurred_on=data.incurred_on,
                    description=data.description,
                    supplier=data.supplier,
                    amount=data.amount,
                    vat_rate_id=data.vat_rate_id,
                    is_billable=data.is_billable,
                    billing_status="unbilled",
                    created_by=ctx.user.id,
                )
            )
            await record(
                self.session,
                ctx,
                rctx,
                "disbursement.created",
                target_type="disbursement",
                target_id=row.id,
                meta={
                    "client_id": str(client.id),
                    "client_name": client.name,
                    "description": row.description,
                    "amount": row.amount,
                    "incurred_on": row.incurred_on.isoformat(),
                },
            )
        return row

    async def update(
        self,
        ctx: OrgContext,
        disbursement_id: uuid.UUID,
        data: DisbursementUpdate,
        rctx: RequestContext,
    ) -> Disbursement:
        fields = data.model_fields_set
        async with unit_of_work(self.session):
            row = await self._locked_writable(ctx, disbursement_id)
            if data.incurred_on is not None:
                await period_locks.assert_open(
                    self.session, ctx.organization_id, data.incurred_on, "expense"
                )
            client_id = (
                data.client_id if "client_id" in fields and data.client_id else row.client_id
            )
            if client_id != row.client_id:
                client = await ensure_client_accepts_work(
                    self.session, ctx.organization_id, client_id
                )
            else:
                client = await get_client_or_404(self.refs, ctx.organization_id, client_id)
            job_id = data.job_id if "job_id" in fields else row.job_id
            if client_id != row.client_id and "job_id" not in fields:
                job_id = None  # the old job belongs to the old client
            if job_id != row.job_id or client_id != row.client_id:
                await self._check_job(ctx, client, job_id)
            if "vat_rate_id" in fields and data.vat_rate_id not in (None, row.vat_rate_id):
                assert data.vat_rate_id is not None
                await VatResolver(self.refs, ctx.organization).chosen(data.vat_rate_id)

            new_values: dict[str, Any] = {"client_id": client_id, "job_id": job_id}
            for field in ("incurred_on", "description", "supplier", "amount", "vat_rate_id"):
                if field in fields:
                    new_values[field] = getattr(data, field)
            if "is_billable" in fields and data.is_billable is not None:
                new_values["is_billable"] = data.is_billable
            changes: dict[str, list[Any]] = {}
            for field, value in new_values.items():
                old = getattr(row, field)
                if old != value:
                    changes[field] = [_plain(old), _plain(value)]
                    setattr(row, field, value)
            if changes:
                await self.session.flush()
                await record(
                    self.session,
                    ctx,
                    rctx,
                    "disbursement.updated",
                    target_type="disbursement",
                    target_id=row.id,
                    meta={
                        "client_id": str(client.id),
                        "client_name": client.name,
                        "description": row.description,
                        "amount": row.amount,
                        "changes": changes,
                    },
                )
            fresh = await self.disbursements.get(ctx.organization_id, row.id)
            assert fresh is not None
        return fresh

    async def delete(
        self, ctx: OrgContext, disbursement_id: uuid.UUID, rctx: RequestContext
    ) -> None:
        async with unit_of_work(self.session):
            row = await self._locked_writable(ctx, disbursement_id)
            client = await self.refs.get_client(ctx.organization_id, row.client_id)
            meta = {
                "client_id": str(row.client_id),
                "client_name": client.name if client else None,
                "description": row.description,
                "amount": row.amount,
                "incurred_on": row.incurred_on.isoformat(),
            }
            row_id = row.id
            await self.disbursements.delete(row)
            await record(
                self.session,
                ctx,
                rctx,
                "disbursement.deleted",
                target_type="disbursement",
                target_id=row_id,
                meta=meta,
            )

    async def _locked_writable(self, ctx: OrgContext, disbursement_id: uuid.UUID) -> Disbursement:
        row = await self.disbursements.get(ctx.organization_id, disbursement_id, for_update=True)
        if row is None:
            raise NotFoundError("Disbursement not found.")
        if not self._can_write(ctx, row):
            raise AuthorizationError()
        if row.billing_status != "unbilled":
            raise conflict(
                "DISBURSEMENT_LOCKED",
                f"This disbursement is {row.billing_status.replace('_', ' ')}: "
                "only unbilled disbursements can be changed.",
            )
        await period_locks.assert_open(
            self.session, ctx.organization_id, row.incurred_on, "expense"
        )
        return row

    async def _check_job(self, ctx: OrgContext, client: Client, job_id: uuid.UUID | None) -> None:
        if job_id is None:
            return
        job = await self.refs.get_job(ctx.organization_id, job_id)
        if job is None or job.client_id != client.id:
            raise ValidationError.for_field("job_id", "The job must be one of the client's jobs.")

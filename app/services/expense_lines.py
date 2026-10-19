"""Validating and pricing one expense line (docs/modules/expenses.md, "Lines").

``assemble`` turns a request into the stored values of a line, whether it is new (``current`` is
None) or an edit of ``current``: the type, date, client, job and VAT code are checked; the amounts
come from net, VAT and gross (any two give the third; one of net or gross takes the VAT from the
VAT code), or from miles x the practice's mileage rate; the billable flag, the markup and the
recharge amount follow. Nothing is written here.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.db.models import Client, Expense, ExpenseType, Job
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.expense_types import ExpenseTypeRepository
from app.schemas.expenses import ExpenseLineCreate, ExpenseLineUpdate
from app.services import policy
from app.services.billing_common import VatResolver, today_for
from app.services.clients import ensure_client_accepts_work
from app.services.expense_rules import (
    Amounts,
    ExpenseJobClosedError,
    mileage_net,
    recharge_for,
    resolve_amounts,
)
from app.services.policy import OrgContext
from app.services.time_rules import OPEN_JOB_STATUSES

type LineInput = ExpenseLineCreate | ExpenseLineUpdate
type VatCode = tuple[uuid.UUID | None, int | None]


@dataclass(frozen=True, slots=True)
class _Priced:
    amounts: Amounts
    miles: Decimal | None
    mileage_rate: int | None
    rate_id: uuid.UUID | None
    rate_bp: int | None


class ExpenseLineBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.types = ExpenseTypeRepository(session)
        self.refs = BillingReferenceRepository(session)

    async def assemble(
        self, ctx: OrgContext, data: LineInput, current: Expense | None
    ) -> dict[str, Any]:
        """The values of the line after the request (all of them, changed or not)."""
        org = ctx.organization
        fields = data.model_fields_set

        def pick(name: str, kept: Any) -> Any:
            return getattr(data, name) if name in fields else kept

        def kept_of(name: str) -> Any:
            return getattr(current, name) if current is not None else None

        expense_type = await self._expense_type(ctx, data, fields, current)
        incurred_on = self._date(ctx, data, fields, current)
        description = pick("description", kept_of("description"))
        supplier = pick("supplier", kept_of("supplier"))
        payer = pick("payer", kept_of("payer") or "staff")
        client, job = await self._client_and_job(ctx, data, fields, current)
        code = await self._vat_code(ctx, data, fields, current)
        priced = await self._price(ctx, expense_type, data, fields, current, code)
        is_billable = self._billable(data, fields, current, expense_type, client, job)

        recharge_given = "recharge_amount" in fields
        if recharge_given and not policy.has_permission(ctx, "expense.manage"):
            raise AuthorizationError("Only people who manage expenses can set the recharge amount.")
        override = data.recharge_amount if recharge_given else None
        net = priced.amounts.net
        reprice = (
            current is None
            or recharge_given
            or net != current.net
            or is_billable != current.is_billable
        )
        if not is_billable:
            if override:
                raise ValidationError.for_field(
                    "recharge_amount",
                    "Only a billable expense has a recharge amount.",
                    "not_billable",
                )
            markup_bp = current.markup_bp if current is not None else org.expense_markup_bp
            recharge = 0
        elif override is not None:
            markup_bp = current.markup_bp if current is not None else org.expense_markup_bp
            recharge = override
        elif reprice:
            markup_bp = org.expense_markup_bp
            recharge = recharge_for(net, markup_bp)
        else:
            assert current is not None
            markup_bp, recharge = current.markup_bp, current.recharge_amount

        return {
            "expense_type_id": expense_type.id,
            "incurred_on": incurred_on,
            "description": description,
            "client_id": client.id if client is not None else None,
            "job_id": job.id if job is not None else None,
            "supplier": supplier,
            "vat_rate_id": priced.rate_id,
            "vat_rate_bp": priced.rate_bp,
            "net": priced.amounts.net,
            "vat": priced.amounts.vat,
            "gross": priced.amounts.gross,
            "miles": priced.miles,
            "mileage_rate_applied": priced.mileage_rate,
            "is_billable": is_billable,
            "markup_bp": markup_bp,
            "recharge_amount": recharge,
            "payer": payer,
        }

    # --- Reference checks ------------------------------------------------------------------

    async def _expense_type(
        self, ctx: OrgContext, data: LineInput, fields: set[str], current: Expense | None
    ) -> ExpenseType:
        type_id = (
            data.expense_type_id
            if "expense_type_id" in fields
            else (current.expense_type_id if current else None)
        )
        assert type_id is not None
        row = await self.types.get(ctx.organization_id, type_id)
        if row is None:
            raise NotFoundError("Expense type not found.", details={"field": "expense_type_id"})
        if (current is None or type_id != current.expense_type_id) and not row.is_active:
            raise ValidationError.for_field(
                "expense_type_id", "This expense type is inactive.", "inactive"
            )
        return row

    @staticmethod
    def _date(ctx: OrgContext, data: LineInput, fields: set[str], current: Expense | None) -> date:
        if "incurred_on" in fields and data.incurred_on is not None:
            if data.incurred_on > today_for(ctx.organization):
                raise ValidationError.for_field(
                    "incurred_on", "The date cannot be in the future.", "future_date"
                )
            return data.incurred_on
        assert current is not None
        return current.incurred_on

    async def _client_and_job(
        self, ctx: OrgContext, data: LineInput, fields: set[str], current: Expense | None
    ) -> tuple[Client | None, Job | None]:
        org_id = ctx.organization_id
        client_id = (
            data.client_id if "client_id" in fields else (current.client_id if current else None)
        )
        job_id = data.job_id if "job_id" in fields else (current.job_id if current else None)
        if (
            current is not None
            and "client_id" in fields
            and client_id != current.client_id
            and "job_id" not in fields
        ):
            job_id = None  # the old job belongs to the old client
        job: Job | None = None
        if job_id is not None:
            job = await self.refs.get_job(org_id, job_id)
            if job is None:
                raise NotFoundError("Job not found.", details={"field": "job_id"})
            if client_id is None:
                client_id = job.client_id
            elif job.client_id != client_id:
                raise ValidationError.for_field(
                    "job_id", "This job belongs to another client.", "job_client_mismatch"
                )
            if (current is None or job_id != current.job_id) and job.status not in (
                OPEN_JOB_STATUSES
            ):
                raise ExpenseJobClosedError(details={"job_id": str(job.id), "status": job.status})
        client: Client | None = None
        if client_id is not None:
            if current is None or client_id != current.client_id:
                try:
                    client = await ensure_client_accepts_work(self.session, org_id, client_id)
                except NotFoundError as exc:
                    raise NotFoundError(
                        "Client not found.", details={"field": "client_id"}
                    ) from exc
            else:
                client = await self.refs.get_client(org_id, client_id)
                if client is None:
                    raise NotFoundError("Client not found.", details={"field": "client_id"})
        return client, job

    async def _vat_code(
        self, ctx: OrgContext, data: LineInput, fields: set[str], current: Expense | None
    ) -> VatCode:
        """The VAT code the request or the stored line names (none when it names none)."""
        if "vat_rate_id" in fields:
            if data.vat_rate_id is None:
                return None, None
            if current is not None and data.vat_rate_id == current.vat_rate_id:
                return current.vat_rate_id, current.vat_rate_bp
            return await VatResolver(self.refs, ctx.organization).chosen(data.vat_rate_id)
        if current is not None:
            return current.vat_rate_id, current.vat_rate_bp
        return None, None

    async def _priced_code(
        self, ctx: OrgContext, expense_type: ExpenseType, code: VatCode
    ) -> VatCode:
        """The code that prices VAT: the line's, else the type's default when it is still live."""
        if code[0] is not None:
            return code
        if expense_type.default_vat_rate_id is None:
            return None, None
        rate = await self.refs.get_vat_rate(ctx.organization_id, expense_type.default_vat_rate_id)
        if rate is None or rate.archived_at is not None:
            return None, None
        return rate.id, rate.rate_bp

    # --- Money -----------------------------------------------------------------------------

    async def _price(
        self,
        ctx: OrgContext,
        expense_type: ExpenseType,
        data: LineInput,
        fields: set[str],
        current: Expense | None,
        code: VatCode,
    ) -> _Priced:
        net_in = data.net if "net" in fields else None
        vat_in = data.vat if "vat" in fields else None
        gross_in = data.gross if "gross" in fields else None
        code_changed = "vat_rate_id" in fields and (
            current is None or data.vat_rate_id != current.vat_rate_id
        )

        if expense_type.is_mileage:
            return await self._price_mileage(
                ctx, expense_type, data, fields, current, code, vat_in, code_changed
            )
        if "miles" in fields:
            raise ValidationError.for_field(
                "miles", "Miles apply to mileage expense types only.", "not_mileage"
            )
        if net_in is None and vat_in is None and gross_in is None:
            if current is None:
                raise ValidationError.for_field(
                    "net", "Give the net or the gross amount.", "required"
                )
            if not code_changed:
                return _Priced(
                    Amounts(current.net, current.vat, current.gross),
                    None,
                    None,
                    current.vat_rate_id,
                    current.vat_rate_bp,
                )
            if code[0] is None:  # the code was removed: the amounts stay
                return _Priced(
                    Amounts(current.net, current.vat, current.gross), None, None, None, None
                )
            amounts = resolve_amounts(net=current.net, vat=None, gross=None, rate_bp=code[1])
            return _Priced(amounts, None, None, *code)
        computes_vat = vat_in is None and (net_in is None or gross_in is None)
        priced_code = await self._priced_code(ctx, expense_type, code) if computes_vat else code
        amounts = resolve_amounts(
            net=net_in,
            vat=vat_in,
            gross=gross_in,
            rate_bp=priced_code[1],
            kept_net=current.net if current is not None else None,
        )
        return _Priced(amounts, None, None, *priced_code)

    async def _price_mileage(
        self,
        ctx: OrgContext,
        expense_type: ExpenseType,
        data: LineInput,
        fields: set[str],
        current: Expense | None,
        code: VatCode,
        vat_in: int | None,
        code_changed: bool,
    ) -> _Priced:
        for name in ("net", "gross"):
            if name in fields and getattr(data, name) is not None:
                raise ValidationError.for_field(
                    name,
                    "Mileage is priced from the miles and the practice's mileage rate.",
                    "mileage_priced",
                )
        miles = data.miles if "miles" in fields else (current.miles if current else None)
        if miles is None:
            raise ValidationError.for_field(
                "miles", "Give the miles for a mileage expense.", "required"
            )
        was_mileage = current is not None and current.miles is not None
        repriced = current is None or "miles" in fields or not was_mileage
        if repriced:
            rate = ctx.organization.mileage_rate
            net = mileage_net(miles, rate)
        else:
            assert current is not None and current.mileage_rate_applied is not None
            rate, net = current.mileage_rate_applied, current.net
        if vat_in is not None:
            amounts = resolve_amounts(net=net, vat=vat_in, gross=None, rate_bp=None)
            return _Priced(amounts, miles, rate, *code)
        if repriced:
            priced_code = await self._priced_code(ctx, expense_type, code)
            amounts = resolve_amounts(net=net, vat=None, gross=None, rate_bp=priced_code[1])
            return _Priced(amounts, miles, rate, *priced_code)
        assert current is not None
        if code_changed and code[0] is not None:
            amounts = resolve_amounts(net=net, vat=None, gross=None, rate_bp=code[1])
            return _Priced(amounts, miles, rate, *code)
        amounts = resolve_amounts(net=net, vat=current.vat, gross=None, rate_bp=None)
        # A removed code leaves the VAT as it is.
        return _Priced(amounts, miles, rate, *code)

    @staticmethod
    def _billable(
        data: LineInput,
        fields: set[str],
        current: Expense | None,
        expense_type: ExpenseType,
        client: Client | None,
        job: Job | None,
    ) -> bool:
        non_billable_job = job is not None and job.billing_basis == "non_billable"
        if "is_billable" not in fields or data.is_billable is None:
            if current is None:
                return expense_type.default_billable and client is not None and not non_billable_job
            return current.is_billable and client is not None
        if data.is_billable and client is None:
            raise ValidationError.for_field(
                "is_billable", "A billable expense needs a client.", "needs_client"
            )
        if data.is_billable and non_billable_job:
            raise ValidationError.for_field(
                "is_billable",
                "An expense on a non-billable job cannot be billable.",
                "non_billable_job",
            )
        return data.is_billable

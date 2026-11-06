"""WIP valuation (ticket APT84): which rate sources value the practice's time, and in what order.

The order is ``organizations.wip_valuation_sources`` and the rate ladder follows it
(``app/services/charge_rates.py``). Changing it can re-value the practice's unbilled time in the
same transaction through the re-rate engine (``app/services/rerate.py``): billed, in-draft,
written-off and period-locked time keeps its value. A preview says what that would do and
saves nothing.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import RequestContext
from app.core.exceptions import ValidationError
from app.db.models import Organization, RateRerun
from app.db.models.organization import WIP_VALUATION_SOURCES
from app.db.session import unit_of_work
from app.schemas.organization import (
    WipValuationPreview,
    WipValuationPreviewRequest,
    WipValuationSkipped,
    WipValuationUpdate,
)
from app.schemas.rate_rules import RerateScope
from app.services import audit
from app.services.organizations import lock_and_refresh
from app.services.policy import OrgContext
from app.services.rerate import MAX_ENTRIES, RerateService, scope_filters

EMPTY_MESSAGE = "Keep at least one rate switched on."
INVALID_MESSAGE = "List client, service, grade and staff, each at most once."
TOO_MANY_MESSAGE = (
    f"More than {MAX_ENTRIES:,} unbilled time entries would be re-valued at once. Save the order "
    "without re-valuing, then re-rate the time in parts in Team > Rates > Change Rates on Past "
    "Time."
)
# Every unbilled time entry of the practice (the request model asks callers to narrow a scope).
ALL_TIME = RerateScope.model_construct()


def _invalid(
    message: str, field: str = "sources", code: str = "WIP_VALUATION_SOURCES"
) -> ValidationError:
    return ValidationError(
        message, code=code, details=[{"field": field, "code": "invalid", "message": message}]
    )


def checked_sources(sources: list[str]) -> list[str]:
    """The order as sent, or 422 ``WIP_VALUATION_SOURCES``."""
    if not sources:
        raise _invalid(EMPTY_MESSAGE)
    if len(set(sources)) != len(sources) or not set(sources) <= set(WIP_VALUATION_SOURCES):
        raise _invalid(INVALID_MESSAGE)
    return list(sources)


class WipValuationService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.rerate = RerateService(session, settings)

    async def _too_many(self, ctx: OrgContext) -> bool:
        count = await self.rerate.repo.count_unbilled(ctx.organization_id, scope_filters(ALL_TIME))
        return count > MAX_ENTRIES

    async def preview(
        self, ctx: OrgContext, data: WipValuationPreviewRequest
    ) -> WipValuationPreview:
        sources = checked_sources(data.sources)
        if await self._too_many(ctx):
            return WipValuationPreview(
                entries_changed=None, wip_before=None, wip_after=None, skipped=None, too_many=True
            )
        totals, skipped = await self.rerate.impact(ctx, ALL_TIME, sources)
        return WipValuationPreview(
            entries_changed=totals.entries_to_change,
            wip_before=totals.charge_value_before,
            wip_after=totals.charge_value_after,
            skipped=WipValuationSkipped(
                billed=skipped.billed,
                in_draft=skipped.in_draft,
                locked=skipped.period_locked,
                written_off=skipped.written_off,
            ),
            too_many=False,
        )

    async def update(
        self, ctx: OrgContext, data: WipValuationUpdate, rctx: RequestContext
    ) -> tuple[Organization, RateRerun | None]:
        """Save the order (audited as ``organization.updated`` when it changes) and, with
        ``revalue``, re-value the unbilled time by it (a re-rate run, audited ``rate.rerun``)."""
        sources = checked_sources(data.sources)
        run: RateRerun | None = None
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "organization.update")
            organization = ctx.organization
            if data.revalue and await self._too_many(ctx):
                raise _invalid(TOO_MANY_MESSAGE, field="revalue", code="WIP_VALUATION_TOO_LARGE")
            old = list(organization.wip_valuation_sources)
            if old != sources:
                organization.wip_valuation_sources = sources
                await self.session.flush()
                await audit.record(
                    self.session,
                    "organization.updated",
                    ctx=rctx,
                    actor_user_id=ctx.user.id,
                    organization_id=organization.id,
                    target_type="organization",
                    target_id=organization.id,
                    meta={"changes": {"wip_valuation_sources": [old, sources]}},
                )
            if data.revalue:
                run = await self.rerate.apply_confirmed(
                    ctx,
                    rctx,
                    ALL_TIME,
                    note="WIP valuation changed",
                    audit_meta={"wip_valuation_sources": sources},
                )
        return organization, run

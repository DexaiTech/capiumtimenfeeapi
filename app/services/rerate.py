"""Re-rating unbilled time (docs/domain.md, "Rates").

Re-rating re-takes the charge-out and cost rate of unbilled time entries from the rate ladder as
it is today. It is a two-step operation:

1. **Preview** (nothing changes): for a scope (clients, jobs, staff and/or a date range) it
   lists each entry's old and new rate and value, the totals, and what is left alone, and
   returns a short-lived ``preview_token`` that names the scope and the exact result.
2. **Apply**: the request carries the same scope and the token. The result is computed again
   under a lock on the entries and must equal the previewed one, otherwise 409 (the rates or
   the entries moved since the preview: preview again). Applying writes one run record
   (``rate_reruns``), one row per entry that changed or was skipped with a reason
   (``rate_rerun_items``) and an audit row.

Only ``unbilled`` entries are touched: billed, in-draft and written-off time never changes,
and neither does time in a locked period (``app/services/period_locks.py``). The V1 endpoint
``POST /rates/revalue`` applies directly, without a preview, through the same engine.
"""

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models import RateRerun, RateRerunItem, TimeEntry
from app.db.session import unit_of_work
from app.repositories.rate_reruns import ItemView, RerateRepository, ScopeFilters
from app.schemas.rate_rules import (
    RefRead,
    RerateApply,
    RerateEntry,
    ReratePreview,
    RerateRequest,
    RerateScope,
    RerateSkipped,
    RerateTotals,
    RerateValues,
    RerunItemRead,
    RerunRead,
    StaffRef,
)
from app.schemas.time import RevalueRequest, RevalueResult
from app.services import audit, charge_rates, period_locks, policy
from app.services.charge_rates import RateQuery
from app.services.job_budget_alerts import refresh_job_budgets
from app.services.policy import OrgContext
from app.utils.ids import new_id
from app.utils.money import time_value
from app.utils.pagination import Page, PageParams

MAX_ENTRIES = 20_000
TOKEN_TTL = timedelta(minutes=30)
_TOKEN_DOMAIN = b"capium-rerate-preview-v1"


def scope_filters(scope: RerateScope) -> ScopeFilters:
    return ScopeFilters(
        client_ids=tuple(sorted(set(scope.client_ids))),
        job_ids=tuple(sorted(set(scope.job_ids))),
        user_ids=tuple(sorted(set(scope.user_ids))),
        date_from=scope.date_from,
        date_to=scope.date_to,
    )


def scope_json(scope: RerateScope) -> dict[str, Any]:
    f = scope_filters(scope)
    return {
        "client_ids": [str(i) for i in f.client_ids],
        "job_ids": [str(i) for i in f.job_ids],
        "user_ids": [str(i) for i in f.user_ids],
        "date_from": f.date_from.isoformat() if f.date_from else None,
        "date_to": f.date_to.isoformat() if f.date_to else None,
    }


def scope_hash(scope: RerateScope) -> str:
    canonical = json.dumps(scope_json(scope), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(slots=True)
class PlanEntry:
    entry: TimeEntry
    new_charge_rate: int
    new_cost_rate: int
    new_charge_value: int
    new_cost_value: int
    new_source: str | None
    # change | unchanged | skipped
    outcome: str
    skip_reason: str | None = None


@dataclass(slots=True)
class Plan:
    entries: list[PlanEntry]
    skipped: dict[str, int]
    fingerprint: str

    @property
    def priced(self) -> list[PlanEntry]:
        return [p for p in self.entries if p.outcome != "skipped"]

    @property
    def changed(self) -> list[PlanEntry]:
        return [p for p in self.entries if p.outcome == "change"]

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())


def _fingerprint(entries: list[PlanEntry], skipped: dict[str, int]) -> str:
    digest = hashlib.sha256()
    for p in entries:
        if p.outcome == "unchanged":
            continue
        e = p.entry
        digest.update(
            f"{e.id}|{p.outcome}|{e.charge_rate}|{p.new_charge_rate}|{e.cost_rate}|"
            f"{p.new_cost_rate}|{p.new_source}\n".encode()
        )
    digest.update(json.dumps(skipped, sort_keys=True).encode())
    digest.update(str(len(entries)).encode())
    return digest.hexdigest()


def _preview_stale(plan: Plan) -> ConflictError:
    return ConflictError(
        "The rates or the time changed since the preview. Preview again before applying.",
        code="PREVIEW_STALE",
        details={
            "entries_to_change": len(plan.changed),
            "charge_value_after": sum(p.new_charge_value for p in plan.priced),
        },
    )


class RerateService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repo = RerateRepository(session)

    # --- Token ----------------------------------------------------------------------------

    def _key(self) -> bytes:
        secret = self.settings.jwt_secret_key.get_secret_value().encode()
        return hmac.new(secret, _TOKEN_DOMAIN, hashlib.sha256).digest()

    def _sign(self, ctx: OrgContext, scope_digest: str, fingerprint: str) -> tuple[str, datetime]:
        expires = datetime.now(UTC) + TOKEN_TTL
        payload = {
            "o": str(ctx.organization_id),
            "u": str(ctx.user.id),
            "s": scope_digest,
            "f": fingerprint,
            "e": int(expires.timestamp()),
        }
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        signature = hmac.new(self._key(), body, hashlib.sha256).hexdigest()
        return f"{body.decode().rstrip('=')}.{signature}", expires

    def _verify(self, ctx: OrgContext, token: str) -> dict[str, Any]:
        """The claims of a valid, unexpired token issued to this caller for this practice."""
        invalid = ConflictError(
            "Preview this scope first: the preview token is missing, invalid or expired.",
            code="PREVIEW_REQUIRED",
        )
        body, _, signature = token.partition(".")
        if not body or not signature:
            raise invalid
        padded = body + "=" * (-len(body) % 4)
        expected = hmac.new(self._key(), padded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise invalid
        try:
            claims = json.loads(base64.urlsafe_b64decode(padded))
        except ValueError as exc:
            raise invalid from exc
        if (
            not isinstance(claims, dict)
            or claims.get("o") != str(ctx.organization_id)
            or claims.get("u") != str(ctx.user.id)
            or int(claims.get("e", 0)) < datetime.now(UTC).timestamp()
        ):
            raise invalid
        return claims

    # --- The plan -------------------------------------------------------------------------

    async def _plan(self, ctx: OrgContext, scope: RerateScope, *, lock: bool) -> Plan:
        org = ctx.organization
        filters = scope_filters(scope)
        if await self.repo.count_unbilled(org.id, filters) > MAX_ENTRIES:
            raise ValidationError.for_field(
                "scope",
                f"This scope covers more than {MAX_ENTRIES} unbilled entries. Narrow it.",
                code="scope_too_large",
            )
        entries = await self.repo.unbilled_entries(org.id, filters, lock=lock)
        others = await self.repo.other_status_counts(org.id, filters)
        locked = await period_locks.locked_dates(
            self.session, org.id, {e.entry_date for e in entries}
        )
        priced = [e for e in entries if e.entry_date not in locked]
        resolved = iter(
            await charge_rates.resolve_many(
                self.session,
                org,
                [RateQuery(e.user_id, e.client_id, e.job_id, e.entry_date) for e in priced],
            )
        )
        plan_entries: list[PlanEntry] = []
        for entry in entries:
            if entry.entry_date in locked:
                plan_entries.append(
                    PlanEntry(
                        entry,
                        entry.charge_rate,
                        entry.cost_rate,
                        entry.charge_value,
                        entry.cost_value,
                        entry.rate_source,
                        "skipped",
                        locked[entry.entry_date],
                    )
                )
                continue
            rate = next(resolved)
            changed = (entry.charge_rate, entry.cost_rate) != (rate.charge_rate, rate.cost_rate)
            plan_entries.append(
                PlanEntry(
                    entry,
                    rate.charge_rate,
                    rate.cost_rate,
                    time_value(entry.minutes, rate.charge_rate),
                    time_value(entry.minutes, rate.cost_rate),
                    rate.source,
                    "change" if changed else "unchanged",
                )
            )
        skipped = {
            "billed": others.get("billed", 0),
            "in_draft": others.get("in_draft", 0),
            "written_off": others.get("written_off", 0),
            "period_locked": sum(1 for e in entries if e.entry_date in locked),
        }
        return Plan(plan_entries, skipped, _fingerprint(plan_entries, skipped))

    @staticmethod
    def _totals(plan: Plan, *, show_cost: bool) -> RerateTotals:
        priced = plan.priced
        charge_before = sum(p.entry.charge_value for p in priced)
        charge_after = sum(p.new_charge_value for p in priced)
        cost_before = sum(p.entry.cost_value for p in priced)
        cost_after = sum(p.new_cost_value for p in priced)
        changed = len(plan.changed)
        return RerateTotals(
            entries_examined=len(priced),
            entries_to_change=changed,
            entries_unchanged=len(priced) - changed,
            entries_skipped=plan.skipped_total,
            charge_value_before=charge_before,
            charge_value_after=charge_after,
            charge_value_delta=charge_after - charge_before,
            cost_value_before=cost_before if show_cost else None,
            cost_value_after=cost_after if show_cost else None,
            cost_value_delta=cost_after - cost_before if show_cost else None,
        )

    # --- Preview --------------------------------------------------------------------------

    async def preview(self, ctx: OrgContext, request: RerateRequest) -> ReratePreview:
        show_cost = policy.has_permission(ctx, "rate.read")
        plan = await self._plan(ctx, request.scope, lock=False)
        token, expires = self._sign(ctx, scope_hash(request.scope), plan.fingerprint)

        listed = [p for p in plan.entries if p.outcome != "unchanged" or request.include_unchanged]
        listed.sort(key=lambda p: (p.entry.entry_date, p.entry.id))
        shown = listed[: request.item_limit]
        staff, clients, jobs = await self.repo.names(
            ctx.organization_id,
            {p.entry.user_id for p in shown},
            {p.entry.client_id for p in shown if p.entry.client_id},
            {p.entry.job_id for p in shown if p.entry.job_id},
        )
        items = [_entry_read(p, staff, clients, jobs, show_cost=show_cost) for p in shown]
        return ReratePreview(
            preview_token=token,
            expires_at=expires,
            scope=request.scope,
            totals=self._totals(plan, show_cost=show_cost),
            skipped=RerateSkipped(**plan.skipped),
            items=items,
            items_truncated=len(listed) > len(shown),
        )

    # --- Apply ----------------------------------------------------------------------------

    async def apply(self, ctx: OrgContext, data: RerateApply, rctx: RequestContext) -> RerunRead:
        digest = scope_hash(data.scope)
        claims = self._verify(ctx, data.preview_token)
        if claims.get("s") != digest:
            raise ConflictError(
                "This is not the scope that was previewed. Preview it again.",
                code="PREVIEW_MISMATCH",
            )
        async with unit_of_work(self.session):
            plan = await self._plan(ctx, data.scope, lock=True)
            if plan.fingerprint != claims.get("f"):
                raise _preview_stale(plan)
            run = await self._persist(
                ctx,
                rctx,
                plan,
                data.scope,
                source="rerate",
                note=data.note,
                audit_action="rate.rerun",
                audit_meta={"scope": scope_json(data.scope), "note": data.note},
            )
        return RerunRead.from_model(
            run, ctx.user.full_name, show_cost=policy.has_permission(ctx, "rate.read")
        )

    async def revalue(
        self, ctx: OrgContext, data: RevalueRequest, rctx: RequestContext
    ) -> RevalueResult:
        """V1: re-price unbilled entries in a date range straight away (no preview)."""
        scope = RerateScope(
            client_ids=[data.client_id] if data.client_id else [],
            user_ids=[data.user_id] if data.user_id else [],
            date_from=data.date_from,
            date_to=data.date_to,
        )
        async with unit_of_work(self.session):
            plan = await self._plan(ctx, scope, lock=True)
            run = await self._persist(
                ctx,
                rctx,
                plan,
                scope,
                source="revalue",
                note=None,
                audit_action="time_entries.revalued",
                audit_meta={
                    "date_from": data.date_from.isoformat(),
                    "date_to": data.date_to.isoformat(),
                    "user_id": str(data.user_id) if data.user_id else None,
                    "client_id": str(data.client_id) if data.client_id else None,
                },
            )
        show_cost = policy.has_permission(ctx, "rate.read")
        return RevalueResult(
            entries_examined=run.entries_examined,
            entries_changed=run.entries_changed,
            charge_value_before=run.charge_value_before,
            charge_value_after=run.charge_value_after,
            value_delta=run.charge_value_after - run.charge_value_before,
            cost_value_delta=run.cost_value_after - run.cost_value_before if show_cost else None,
        )

    async def _persist(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        plan: Plan,
        scope: RerateScope,
        *,
        source: str,
        note: str | None,
        audit_action: str,
        audit_meta: dict[str, Any],
    ) -> RateRerun:
        """Write the new rates onto the changed entries, the run record, its items and the
        audit row. Inside the caller's unit of work, with the entries locked."""
        org_id = ctx.organization_id
        priced = plan.priced
        run = RateRerun(
            id=new_id(),
            organization_id=org_id,
            source=source,
            scope=scope_json(scope),
            scope_hash=scope_hash(scope),
            entries_examined=len(priced),
            entries_changed=len(plan.changed),
            entries_skipped=plan.skipped_total,
            skipped=plan.skipped,
            charge_value_before=sum(p.entry.charge_value for p in priced),
            charge_value_after=sum(p.new_charge_value for p in priced),
            cost_value_before=sum(p.entry.cost_value for p in priced),
            cost_value_after=sum(p.new_cost_value for p in priced),
            requested_by=ctx.user.id,
            note=note,
        )
        items: list[RateRerunItem] = []
        for p in plan.entries:
            if p.outcome == "unchanged":
                continue
            e = p.entry
            items.append(
                RateRerunItem(
                    organization_id=org_id,
                    run_id=run.id,
                    time_entry_id=e.id,
                    user_id=e.user_id,
                    client_id=e.client_id,
                    job_id=e.job_id,
                    entry_date=e.entry_date,
                    minutes=e.minutes,
                    outcome="changed" if p.outcome == "change" else "skipped",
                    skip_reason=p.skip_reason,
                    old_charge_rate=e.charge_rate,
                    new_charge_rate=p.new_charge_rate,
                    old_charge_value=e.charge_value,
                    new_charge_value=p.new_charge_value,
                    old_cost_rate=e.cost_rate,
                    new_cost_rate=p.new_cost_rate,
                    old_cost_value=e.cost_value,
                    new_cost_value=p.new_cost_value,
                    old_source=e.rate_source,
                    new_source=p.new_source,
                )
            )
            if p.outcome == "change":
                e.charge_rate = p.new_charge_rate
                e.cost_rate = p.new_cost_rate
                e.charge_value = p.new_charge_value
                e.cost_value = p.new_cost_value
                e.rate_source = p.new_source
        await self.repo.add_run(run, items)
        # New charge values move the fee budget usage of the jobs of the entries that changed.
        await refresh_job_budgets(
            self.session,
            org_id,
            [p.entry.job_id for p in plan.entries if p.outcome == "change"],
            actor_user_id=ctx.user.id,
            rctx=rctx,
        )
        await audit.record(
            self.session,
            audit_action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=org_id,
            target_type="rate_rerun" if source == "rerate" else "time_entry",
            target_id=run.id if source == "rerate" else None,
            meta={
                **audit_meta,
                "run_id": str(run.id),
                "entries_examined": run.entries_examined,
                "entries_changed": run.entries_changed,
                "entries_skipped": run.entries_skipped,
                "charge_value_before": run.charge_value_before,
                "charge_value_after": run.charge_value_after,
                "cost_value_delta": run.cost_value_after - run.cost_value_before,
            },
        )
        return run

    # --- Runs -----------------------------------------------------------------------------

    async def list_runs(self, ctx: OrgContext, params: PageParams) -> Page[RerunRead]:
        show_cost = policy.has_permission(ctx, "rate.read")
        page = await self.repo.list_runs(ctx.organization_id, params)
        return Page(
            items=[
                RerunRead.from_model(run, name, show_cost=show_cost) for run, name in page.items
            ],
            next_cursor=page.next_cursor,
        )

    async def get_run(self, ctx: OrgContext, run_id: uuid.UUID) -> RerunRead:
        found = await self.repo.get_run(ctx.organization_id, run_id)
        if found is None:
            raise NotFoundError("Re-rate run not found.")
        return RerunRead.from_model(
            found[0], found[1], show_cost=policy.has_permission(ctx, "rate.read")
        )

    async def list_items(
        self, ctx: OrgContext, run_id: uuid.UUID, *, outcome: str | None, params: PageParams
    ) -> Page[RerunItemRead]:
        if await self.repo.get_run(ctx.organization_id, run_id) is None:
            raise NotFoundError("Re-rate run not found.")
        show_cost = policy.has_permission(ctx, "rate.read")
        page = await self.repo.list_items(
            ctx.organization_id, run_id, outcome=outcome, params=params
        )
        return Page(
            items=[_item_read(v, show_cost=show_cost) for v in page.items],
            next_cursor=page.next_cursor,
        )


def _values(
    charge_rate: int,
    charge_value: int,
    cost_rate: int,
    cost_value: int,
    source: str | None,
    *,
    show_cost: bool,
) -> RerateValues:
    return RerateValues(
        charge_rate=charge_rate,
        charge_value=charge_value,
        cost_rate=cost_rate if show_cost else None,
        cost_value=cost_value if show_cost else None,
        source=source,
    )


def _entry_read(
    p: PlanEntry,
    staff: dict[uuid.UUID, tuple[str | None, str]],
    clients: dict[uuid.UUID, tuple[str, str]],
    jobs: dict[uuid.UUID, tuple[str, str]],
    *,
    show_cost: bool,
) -> RerateEntry:
    e = p.entry
    who = staff.get(e.user_id)
    client = clients.get(e.client_id) if e.client_id else None
    job = jobs.get(e.job_id) if e.job_id else None
    return RerateEntry(
        entry_id=e.id,
        entry_date=e.entry_date,
        minutes=e.minutes,
        staff=StaffRef(id=e.user_id, code=who[0], full_name=who[1]) if who else None,
        client=RefRead(id=e.client_id, code=client[0], name=client[1])
        if client and e.client_id
        else None,
        job=RefRead(id=e.job_id, code=job[0], name=job[1]) if job and e.job_id else None,
        outcome=p.outcome,
        skip_reason=p.skip_reason,
        old=_values(
            e.charge_rate,
            e.charge_value,
            e.cost_rate,
            e.cost_value,
            e.rate_source,
            show_cost=show_cost,
        ),
        new=_values(
            p.new_charge_rate,
            p.new_charge_value,
            p.new_cost_rate,
            p.new_cost_value,
            p.new_source,
            show_cost=show_cost,
        ),
    )


def _item_read(view: ItemView, *, show_cost: bool) -> RerunItemRead:
    item = view.item
    return RerunItemRead.from_model(
        item,
        staff=StaffRef(id=item.user_id, code=view.staff_code, full_name=view.user_name)
        if view.user_name
        else None,
        client=RefRead(id=item.client_id, code=view.client_code, name=view.client_name)
        if item.client_id and view.client_name
        else None,
        job=RefRead(id=item.job_id, code=view.job_number, name=view.job_name)
        if item.job_id and view.job_name
        else None,
        show_cost=show_cost,
    )

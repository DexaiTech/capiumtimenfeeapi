"""Writing the WIP ledger and asking what a part of the WIP is worth today.

The ledger (``wip_movements``) is append-only: this is the only code that inserts into it, and it
never updates or deletes. A correction is a ``reversal`` row.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.db.models import Client, Job, Organization, WipMovement
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.wip_atoms import WipAtomRepository, WipFilters
from app.repositories.wip_ledger import MovementFilters, WipLedgerRepository
from app.schemas.billing import ClientRef, PersonRef
from app.schemas.wip import MovementRead, WipJobRef
from app.services.policy import OrgContext
from app.services.reports import practice_today
from app.utils.ids import new_id
from app.utils.pagination import Page, PageParams


def default_component(job: Job | None) -> str:
    """Which WIP an amount adjusts when the person does not say: the fee on a fixed-fee job,
    time otherwise."""
    return "fixed_fee" if job is not None and job.billing_basis == "fixed" else "time"


def effective_date_of(organization: Organization, requested: date | None) -> date:
    """The date a movement takes effect: today by default, never in the future."""
    today = practice_today(organization)
    day = requested or today
    if day > today:
        raise ValidationError.for_field(
            "effective_date", "The date cannot be in the future.", code="future_date"
        )
    return day


@dataclass(slots=True)
class Lookup:
    """The clients, jobs and people a list of records refers to, for the response models."""

    clients: dict[uuid.UUID, Client]
    jobs: dict[uuid.UUID, Job]
    names: dict[uuid.UUID, str]

    def client(self, client_id: uuid.UUID) -> ClientRef | None:
        found = self.clients.get(client_id)
        return ClientRef.from_model(found) if found else None

    def job(self, job_id: uuid.UUID | None) -> WipJobRef | None:
        found = self.jobs.get(job_id) if job_id else None
        if found is None:
            return None
        return WipJobRef(
            id=found.id, number=found.number, name=found.name, billing_basis=found.billing_basis
        )

    def person(self, user_id: uuid.UUID | None) -> PersonRef | None:
        if user_id is None:
            return None
        return PersonRef(user_id=user_id, full_name=self.names.get(user_id))


async def lookup_refs(
    refs: BillingReferenceRepository,
    organization_id: uuid.UUID,
    *,
    client_ids: Iterable[uuid.UUID],
    job_ids: Iterable[uuid.UUID | None],
    user_ids: Iterable[uuid.UUID | None],
) -> Lookup:
    return Lookup(
        clients=await refs.clients_by_ids(organization_id, client_ids),
        jobs=await refs.jobs_by_ids(organization_id, job_ids),
        names=await refs.member_names(organization_id, user_ids),
    )


class WipLedger:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = WipLedgerRepository(session)
        self.atoms = WipAtomRepository(session)

    async def scope_value(
        self,
        organization: Organization,
        client_id: uuid.UUID,
        job_id: uuid.UUID | None,
        component: str,
    ) -> int:
        """What the job's WIP of one component (or the client's job-less WIP) is worth today."""
        today = practice_today(organization)
        atoms = await self.atoms.load_atoms(
            organization.id,
            as_at=today,
            today=today,
            timezone=organization.timezone,
            require_approval=organization.require_timesheet_approval,
            fixed_fee_mode=organization.fixed_fee_wip_mode,
            filters=WipFilters(client_id=client_id, job_id=job_id),
        )
        return sum(a.value for a in atoms if a.job_id == job_id and a.component == component)

    async def in_force(self, organization: Organization) -> set[uuid.UUID]:
        """Ids of the amount movements that count in today's WIP (not reversed, not settled)."""
        today = practice_today(organization)
        atoms = await self.atoms.adjustment_atoms(organization.id, today, today, WipFilters())
        return {a.id for a in atoms}

    async def post(self, organization_id: uuid.UUID, **fields: Any) -> WipMovement:
        """Insert one movement (the arguments of :meth:`build`) and read it back."""
        return await self.repo.add_movement(self.build(organization_id, **fields))

    @staticmethod
    def build(
        organization_id: uuid.UUID,
        *,
        kind: str,
        scope: str,
        component: str,
        client_id: uuid.UUID,
        job_id: uuid.UUID | None,
        amount: int,
        effective_date: date,
        reason: str,
        actor_user_id: uuid.UUID | None,
        approved_by: uuid.UUID | None = None,
        approved_at: datetime | None = None,
        reversal_of_id: uuid.UUID | None = None,
        source_type: str | None = None,
        source_id: uuid.UUID | None = None,
        item_count: int = 0,
        item_refs: list[dict[str, Any]] | None = None,
    ) -> WipMovement:
        return WipMovement(
            id=new_id(),
            organization_id=organization_id,
            kind=kind,
            scope=scope,
            component=component,
            client_id=client_id,
            job_id=job_id,
            amount=amount,
            effective_date=effective_date,
            reason=reason,
            actor_user_id=actor_user_id,
            approval_state="approved" if approved_by else "not_required",
            approved_by=approved_by,
            approved_at=approved_at,
            reversal_of_id=reversal_of_id,
            source_type=source_type,
            source_id=source_id,
            item_count=item_count,
            item_refs=item_refs or [],
        )


class WipMovementService:
    """Reads of the ledger: every movement, newest first."""

    def __init__(self, session: AsyncSession) -> None:
        self.repo = WipLedgerRepository(session)
        self.refs = BillingReferenceRepository(session)

    async def list(
        self, ctx: OrgContext, f: MovementFilters, params: PageParams
    ) -> Page[MovementRead]:
        page = await self.repo.list_movements(ctx.organization_id, f, params)
        rows = page.items
        cancelled = await self.repo.reversed_ids(ctx.organization_id, [m.id for m in rows])
        lookup = await lookup_refs(
            self.refs,
            ctx.organization_id,
            client_ids={m.client_id for m in rows},
            job_ids=[m.job_id for m in rows],
            user_ids=[u for m in rows for u in (m.actor_user_id, m.approved_by)],
        )
        return Page(
            items=[
                MovementRead(
                    id=m.id,
                    kind=m.kind,
                    scope=m.scope,
                    component=m.component,
                    client=lookup.client(m.client_id),
                    job=lookup.job(m.job_id),
                    amount=m.amount,
                    effective_date=m.effective_date,
                    reason=m.reason,
                    actor=lookup.person(m.actor_user_id),
                    approval_state=m.approval_state,
                    approved_by=lookup.person(m.approved_by),
                    approved_at=m.approved_at,
                    reversal_of_id=m.reversal_of_id,
                    reversed=m.id in cancelled,
                    source_type=m.source_type,
                    source_id=m.source_id,
                    item_count=m.item_count,
                    created_at=m.created_at,
                )
                for m in rows
            ],
            next_cursor=page.next_cursor,
        )

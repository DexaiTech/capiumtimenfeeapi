"""Practice settings queries: VAT rates, services, activity codes and number sequences.

Every method is scoped to one organization.
"""

import uuid
from collections.abc import Collection

from sqlalchemy import delete, exists, func, or_, select, union, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ActivityCode,
    Client,
    CreditNote,
    CreditNoteLine,
    Disbursement,
    DocumentSequence,
    Expense,
    ExpenseType,
    Invoice,
    InvoiceLine,
    Job,
    RateRule,
    Service,
    ServiceActivityCode,
    ServiceTaskTemplate,
    Ticket,
    TimeEntry,
    Timer,
    VatRate,
)
from app.db.models.clients import ClientService


class VatRateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self, organization_id: uuid.UUID, *, include_archived: bool) -> list[VatRate]:
        stmt = select(VatRate).where(VatRate.organization_id == organization_id)
        if not include_archived:
            stmt = stmt.where(VatRate.archived_at.is_(None))
        stmt = stmt.order_by(VatRate.archived_at.is_not(None), VatRate.rate_bp.desc(), VatRate.name)
        return list((await self.session.execute(stmt)).scalars())

    async def get(
        self, organization_id: uuid.UUID, rate_id: uuid.UUID, *, lock: bool = False
    ) -> VatRate | None:
        stmt = select(VatRate).where(
            VatRate.organization_id == organization_id, VatRate.id == rate_id
        )
        if lock:
            stmt = stmt.with_for_update()
        stmt = stmt.execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_default(self, organization_id: uuid.UUID) -> VatRate | None:
        stmt = select(VatRate).where(
            VatRate.organization_id == organization_id,
            VatRate.is_default.is_(True),
            VatRate.archived_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_all(self, organization_id: uuid.UUID) -> None:
        """Lock every VAT rate of the practice, in id order, to serialise default changes."""
        await self.session.execute(
            select(VatRate.id)
            .where(VatRate.organization_id == organization_id)
            .order_by(VatRate.id)
            .with_for_update()
        )

    async def name_taken(
        self, organization_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(VatRate.id).where(
            VatRate.organization_id == organization_id,
            func.lower(VatRate.name) == name.lower(),
            VatRate.archived_at.is_(None),
        )
        if exclude_id is not None:
            stmt = stmt.where(VatRate.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def clear_default(self, organization_id: uuid.UUID, keep_id: uuid.UUID | None) -> None:
        """Unset the default flag on every rate but ``keep_id`` (a fresh statement, so it also
        sees a default committed by a transaction this one waited for)."""
        stmt = update(VatRate).where(
            VatRate.organization_id == organization_id, VatRate.is_default.is_(True)
        )
        if keep_id is not None:
            stmt = stmt.where(VatRate.id != keep_id)
        await self.session.execute(stmt.values(is_default=False))

    async def is_used(self, organization_id: uuid.UUID, rate_id: uuid.UUID) -> bool:
        checks = [
            exists().where(
                Service.organization_id == organization_id, Service.vat_rate_id == rate_id
            ),
            exists().where(
                Client.organization_id == organization_id, Client.default_vat_rate_id == rate_id
            ),
            exists().where(
                InvoiceLine.organization_id == organization_id, InvoiceLine.vat_rate_id == rate_id
            ),
            exists().where(
                CreditNoteLine.organization_id == organization_id,
                CreditNoteLine.vat_rate_id == rate_id,
            ),
            exists().where(
                Disbursement.organization_id == organization_id,
                Disbursement.vat_rate_id == rate_id,
            ),
            exists().where(
                ActivityCode.organization_id == organization_id,
                ActivityCode.vat_rate_id == rate_id,
            ),
            exists().where(
                ExpenseType.organization_id == organization_id,
                ExpenseType.default_vat_rate_id == rate_id,
            ),
            exists().where(
                Expense.organization_id == organization_id, Expense.vat_rate_id == rate_id
            ),
        ]
        return bool(await self.session.scalar(select(or_(*checks))))

    async def add(self, rate: VatRate) -> VatRate:
        self.session.add(rate)
        await self.session.flush()
        await self.session.refresh(rate)
        return rate

    async def delete(self, organization_id: uuid.UUID, rate_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(VatRate).where(VatRate.organization_id == organization_id, VatRate.id == rate_id)
        )

    async def save(self, rate: VatRate) -> VatRate:
        await self.session.flush()
        await self.session.refresh(rate)
        return rate


class ServiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_services(
        self, organization_id: uuid.UUID, *, include_inactive: bool, q: str | None = None
    ) -> list[Service]:
        stmt = select(Service).where(Service.organization_id == organization_id)
        if not include_inactive:
            stmt = stmt.where(Service.is_active.is_(True))
        if q:
            stmt = stmt.where(
                or_(
                    Service.name.icontains(q, autoescape=True),
                    Service.code.icontains(q, autoescape=True),
                    Service.description.icontains(q, autoescape=True),
                )
            )
        stmt = stmt.order_by(Service.sort_order, Service.name, Service.id)
        return list((await self.session.execute(stmt)).scalars())

    async def get(
        self, organization_id: uuid.UUID, service_id: uuid.UUID, *, lock: bool = False
    ) -> Service | None:
        stmt = select(Service).where(
            Service.organization_id == organization_id, Service.id == service_id
        )
        if lock:
            stmt = stmt.with_for_update()
        stmt = stmt.execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def code_taken(
        self, organization_id: uuid.UUID, code: str, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(Service.id).where(
            Service.organization_id == organization_id, Service.code == code
        )
        if exclude_id is not None:
            stmt = stmt.where(Service.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def by_codes(self, organization_id: uuid.UUID, codes: set[str]) -> dict[str, Service]:
        if not codes:
            return {}
        stmt = select(Service).where(
            Service.organization_id == organization_id, Service.code.in_(codes)
        )
        return {s.code: s for s in (await self.session.execute(stmt)).scalars()}

    async def name_taken(
        self, organization_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(Service.id).where(
            Service.organization_id == organization_id, func.lower(Service.name) == name.lower()
        )
        if exclude_id is not None:
            stmt = stmt.where(Service.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def is_used(self, organization_id: uuid.UUID, service_id: uuid.UUID) -> bool:
        checks = [
            exists().where(Job.organization_id == organization_id, Job.service_id == service_id),
            exists().where(
                ClientService.organization_id == organization_id,
                ClientService.service_id == service_id,
            ),
            exists().where(
                InvoiceLine.organization_id == organization_id,
                InvoiceLine.service_id == service_id,
            ),
            exists().where(
                RateRule.organization_id == organization_id, RateRule.service_id == service_id
            ),
            exists().where(
                Client.organization_id == organization_id, Client.default_service_id == service_id
            ),
        ]
        return bool(await self.session.scalar(select(or_(*checks))))

    async def used_ids(
        self, organization_id: uuid.UUID, service_ids: Collection[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these services jobs, client engagements, invoice lines or rates refer to."""
        if not service_ids:
            return set()
        ids = list(service_ids)
        parts = [
            select(Job.service_id).where(
                Job.organization_id == organization_id, Job.service_id.in_(ids)
            ),
            select(ClientService.service_id).where(
                ClientService.organization_id == organization_id,
                ClientService.service_id.in_(ids),
            ),
            select(InvoiceLine.service_id).where(
                InvoiceLine.organization_id == organization_id, InvoiceLine.service_id.in_(ids)
            ),
            select(RateRule.service_id).where(
                RateRule.organization_id == organization_id, RateRule.service_id.in_(ids)
            ),
            select(Client.default_service_id).where(
                Client.organization_id == organization_id, Client.default_service_id.in_(ids)
            ),
        ]
        rows = (await self.session.execute(union(*parts))).scalars()
        return {service_id for service_id in rows if service_id is not None}

    async def activity_ids(
        self, organization_id: uuid.UUID, service_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        """The activity codes each service is limited to (a service with none is not limited)."""
        if not service_ids:
            return {}
        stmt = (
            select(ServiceActivityCode.service_id, ServiceActivityCode.activity_code_id)
            .where(
                ServiceActivityCode.organization_id == organization_id,
                ServiceActivityCode.service_id.in_(list(service_ids)),
            )
            .order_by(ServiceActivityCode.service_id, ServiceActivityCode.activity_code_id)
        )
        linked: dict[uuid.UUID, list[uuid.UUID]] = {}
        for service_id, activity_id in (await self.session.execute(stmt)).all():
            linked.setdefault(service_id, []).append(activity_id)
        return linked

    async def replace_activity_ids(
        self, organization_id: uuid.UUID, service_id: uuid.UUID, activity_ids: Collection[uuid.UUID]
    ) -> None:
        await self.session.execute(
            delete(ServiceActivityCode).where(
                ServiceActivityCode.organization_id == organization_id,
                ServiceActivityCode.service_id == service_id,
            )
        )
        self.session.add_all(
            ServiceActivityCode(
                organization_id=organization_id, service_id=service_id, activity_code_id=a
            )
            for a in activity_ids
        )
        await self.session.flush()

    async def template_counts(
        self, organization_id: uuid.UUID, service_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """How many default tasks each service has (a service without any is left out)."""
        if not service_ids:
            return {}
        stmt = (
            select(ServiceTaskTemplate.service_id, func.count())
            .where(
                ServiceTaskTemplate.organization_id == organization_id,
                ServiceTaskTemplate.service_id.in_(list(service_ids)),
            )
            .group_by(ServiceTaskTemplate.service_id)
        )
        return {sid: int(n) for sid, n in (await self.session.execute(stmt)).all()}

    async def list_templates(
        self, organization_id: uuid.UUID, service_id: uuid.UUID
    ) -> list[ServiceTaskTemplate]:
        stmt = (
            select(ServiceTaskTemplate)
            .where(
                ServiceTaskTemplate.organization_id == organization_id,
                ServiceTaskTemplate.service_id == service_id,
            )
            .order_by(ServiceTaskTemplate.sort_order, ServiceTaskTemplate.created_at)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def replace_templates(
        self,
        organization_id: uuid.UUID,
        service_id: uuid.UUID,
        items: list[ServiceTaskTemplate],
    ) -> list[ServiceTaskTemplate]:
        await self.session.execute(
            delete(ServiceTaskTemplate).where(
                ServiceTaskTemplate.organization_id == organization_id,
                ServiceTaskTemplate.service_id == service_id,
            )
        )
        self.session.add_all(items)
        await self.session.flush()
        return await self.list_templates(organization_id, service_id)

    async def add(self, service: Service) -> Service:
        self.session.add(service)
        await self.session.flush()
        await self.session.refresh(service)
        return service

    async def delete(self, organization_id: uuid.UUID, service_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(Service).where(
                Service.organization_id == organization_id, Service.id == service_id
            )
        )

    async def save(self, service: Service) -> Service:
        await self.session.flush()
        await self.session.refresh(service)
        return service


class ActivityCodeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_activity_codes(
        self, organization_id: uuid.UUID, *, include_inactive: bool, q: str | None = None
    ) -> list[ActivityCode]:
        stmt = select(ActivityCode).where(ActivityCode.organization_id == organization_id)
        if not include_inactive:
            stmt = stmt.where(ActivityCode.is_active.is_(True))
        if q:
            stmt = stmt.where(
                or_(
                    ActivityCode.name.icontains(q, autoescape=True),
                    ActivityCode.code.icontains(q, autoescape=True),
                )
            )
        stmt = stmt.order_by(ActivityCode.is_chargeable.desc(), ActivityCode.code)
        return list((await self.session.execute(stmt)).scalars())

    async def get(
        self, organization_id: uuid.UUID, activity_id: uuid.UUID, *, lock: bool = False
    ) -> ActivityCode | None:
        stmt = select(ActivityCode).where(
            ActivityCode.organization_id == organization_id, ActivityCode.id == activity_id
        )
        if lock:
            stmt = stmt.with_for_update()
        stmt = stmt.execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def code_taken(
        self, organization_id: uuid.UUID, code: str, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(ActivityCode.id).where(
            ActivityCode.organization_id == organization_id, ActivityCode.code == code
        )
        if exclude_id is not None:
            stmt = stmt.where(ActivityCode.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def is_used(self, organization_id: uuid.UUID, activity_id: uuid.UUID) -> bool:
        checks = [
            exists().where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.activity_code_id == activity_id,
            ),
            exists().where(
                Timer.organization_id == organization_id, Timer.activity_code_id == activity_id
            ),
            exists().where(
                ServiceActivityCode.organization_id == organization_id,
                ServiceActivityCode.activity_code_id == activity_id,
            ),
        ]
        return bool(await self.session.scalar(select(or_(*checks))))

    async def used_ids(
        self, organization_id: uuid.UUID, activity_ids: Collection[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these codes time entries or timers use, or a service lists."""
        if not activity_ids:
            return set()
        ids = list(activity_ids)
        parts = [
            select(TimeEntry.activity_code_id).where(
                TimeEntry.organization_id == organization_id, TimeEntry.activity_code_id.in_(ids)
            ),
            select(Timer.activity_code_id).where(
                Timer.organization_id == organization_id, Timer.activity_code_id.in_(ids)
            ),
            select(ServiceActivityCode.activity_code_id).where(
                ServiceActivityCode.organization_id == organization_id,
                ServiceActivityCode.activity_code_id.in_(ids),
            ),
        ]
        rows = (await self.session.execute(union(*parts))).scalars()
        return {activity_id for activity_id in rows if activity_id is not None}

    async def name_taken(
        self, organization_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(ActivityCode.id).where(
            ActivityCode.organization_id == organization_id,
            func.lower(ActivityCode.name) == name.lower(),
        )
        if exclude_id is not None:
            stmt = stmt.where(ActivityCode.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def get_many(
        self, organization_id: uuid.UUID, activity_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, ActivityCode]:
        if not activity_ids:
            return {}
        stmt = select(ActivityCode).where(
            ActivityCode.organization_id == organization_id,
            ActivityCode.id.in_(list(activity_ids)),
        )
        return {a.id: a for a in (await self.session.execute(stmt)).scalars()}

    async def add(self, activity: ActivityCode) -> ActivityCode:
        self.session.add(activity)
        await self.session.flush()
        await self.session.refresh(activity)
        return activity

    async def delete(self, organization_id: uuid.UUID, activity_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(ActivityCode).where(
                ActivityCode.organization_id == organization_id, ActivityCode.id == activity_id
            )
        )

    async def save(self, activity: ActivityCode) -> ActivityCode:
        await self.session.flush()
        await self.session.refresh(activity)
        return activity


class SequenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self, organization_id: uuid.UUID) -> list[DocumentSequence]:
        stmt = (
            select(DocumentSequence)
            .where(DocumentSequence.organization_id == organization_id)
            .order_by(DocumentSequence.kind)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def lock(self, organization_id: uuid.UUID, kind: str) -> DocumentSequence | None:
        stmt = (
            select(DocumentSequence)
            .where(
                DocumentSequence.organization_id == organization_id,
                DocumentSequence.kind == kind,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, sequence: DocumentSequence) -> DocumentSequence:
        self.session.add(sequence)
        await self.session.flush()
        await self.session.refresh(sequence)
        return sequence

    async def save(self, sequence: DocumentSequence) -> DocumentSequence:
        await self.session.flush()
        await self.session.refresh(sequence)
        return sequence

    async def number_used(self, organization_id: uuid.UUID, kind: str, number: str) -> bool:
        """Whether a document of ``kind`` already carries ``number``."""
        if kind == "invoice":
            stmt = select(Invoice.id).where(
                Invoice.organization_id == organization_id, Invoice.number == number
            )
        elif kind == "credit_note":
            stmt = select(CreditNote.id).where(
                CreditNote.organization_id == organization_id, CreditNote.number == number
            )
        elif kind == "job":
            stmt = select(Job.id).where(
                Job.organization_id == organization_id, Job.number == number
            )
        elif kind == "ticket":
            stmt = select(Ticket.id).where(
                Ticket.organization_id == organization_id, Ticket.code == number
            )
        elif kind == "estimate":
            # Estimates are not stored yet (the estimates module adds the table): no collision.
            return False
        else:
            stmt = select(Client.id).where(
                Client.organization_id == organization_id,
                Client.code == number.upper(),
                Client.deleted_at.is_(None),
            )
        return (await self.session.execute(stmt.limit(1))).first() is not None

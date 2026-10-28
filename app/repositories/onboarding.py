"""Onboarding checklist queries: what the practice's data says, and the decisions people made.

Every method is scoped to one organization. The counts are capped (``COUNT_CAP``) so a large
practice costs no more than a small one: a checklist only needs to know "none", "some" or "a
lot".
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, ScalarSelect, and_, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ActivityCode,
    Client,
    DocumentSequence,
    Grade,
    InvoiceTemplate,
    Job,
    OnboardingProgress,
    OrganizationMember,
    PeriodLockEvent,
    RateRule,
    Service,
    TimeEntry,
    User,
)
from app.utils.ids import new_id

COUNT_CAP = 1000


@dataclass(frozen=True, slots=True)
class OnboardingFacts:
    """What the practice's data says. Counts stop at ``COUNT_CAP``."""

    active_staff: int
    grades_with_rate: int
    charge_rules: int
    services: int
    activity_types: int
    clients: int
    jobs: int
    linked_clients: int
    has_time_entry: bool
    has_period_lock_event: bool
    # A prefix, padding or next number differs from the starting values, or a number was used.
    numbering_changed: bool
    # A template other than the seeded "Standard" one, or that one edited.
    templates_customised: bool


@dataclass(frozen=True, slots=True)
class ManualStep:
    progress: OnboardingProgress
    updated_by_name: str | None


class OnboardingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def facts(
        self,
        organization_id: uuid.UUID,
        *,
        default_template_name: str,
        default_sequences: Mapping[str, tuple[str, int]],
        numbered_kinds: tuple[str, ...],
    ) -> OnboardingFacts:
        org = organization_id

        def capped(model: Any, *where: ColumnElement[bool]) -> ScalarSelect[int]:
            inner = (
                select(literal(1))
                .select_from(model)
                .where(model.organization_id == org, *where)
                .limit(COUNT_CAP)
                .subquery()
            )
            return select(func.count()).select_from(inner).scalar_subquery()

        sequence_changed = or_(
            *(
                and_(
                    DocumentSequence.kind == kind,
                    or_(
                        DocumentSequence.prefix != prefix,
                        DocumentSequence.suffix != "",
                        DocumentSequence.padding != padding,
                        DocumentSequence.next_number > 1,
                    ),
                )
                for kind, (prefix, padding) in default_sequences.items()
                if kind in numbered_kinds
            )
        )
        template_changed = or_(
            InvoiceTemplate.name != default_template_name,
            InvoiceTemplate.layout != "standard",
            InvoiceTemplate.footer_text.is_not(None),
            InvoiceTemplate.show_logo.is_(False),
            InvoiceTemplate.show_bank_details.is_(False),
        )
        stmt = select(
            capped(OrganizationMember, OrganizationMember.status == "active").label("active_staff"),
            capped(Grade, Grade.is_active.is_(True), Grade.default_charge_rate.is_not(None)).label(
                "grades_with_rate"
            ),
            capped(RateRule, RateRule.kind == "charge").label("charge_rules"),
            capped(Service, Service.is_active.is_(True)).label("services"),
            capped(ActivityCode, ActivityCode.is_active.is_(True)).label("activity_types"),
            capped(Client, Client.deleted_at.is_(None)).label("clients"),
            capped(Job, Job.deleted_at.is_(None)).label("jobs"),
            capped(
                Client, Client.deleted_at.is_(None), Client.central_client_id.is_not(None)
            ).label("linked_clients"),
            exists().where(TimeEntry.organization_id == org).label("has_time_entry"),
            exists().where(PeriodLockEvent.organization_id == org).label("has_period_lock_event"),
            exists()
            .where(DocumentSequence.organization_id == org, sequence_changed)
            .label("numbering_changed"),
            exists()
            .where(
                InvoiceTemplate.organization_id == org,
                InvoiceTemplate.deleted_at.is_(None),
                template_changed,
            )
            .label("templates_customised"),
        )
        row = (await self.session.execute(stmt)).one()
        return OnboardingFacts(**row._mapping)

    async def manual_steps(self, organization_id: uuid.UUID) -> dict[str, ManualStep]:
        """The decisions made by hand, by step key (a taken-back ``todo`` is a decision too)."""
        stmt = (
            select(OnboardingProgress, User.full_name)
            .outerjoin(User, User.id == OnboardingProgress.updated_by)
            .where(OnboardingProgress.organization_id == organization_id)
            .execution_options(populate_existing=True)
        )
        rows = await self.session.execute(stmt)
        return {row[0].step_key: ManualStep(row[0], row[1]) for row in rows}

    async def lock_step(self, organization_id: uuid.UUID, step_key: str) -> OnboardingProgress:
        """The step's row ``FOR UPDATE``, created (as ``todo``) first when there is none."""
        await self.session.execute(
            insert(OnboardingProgress)
            .values(id=new_id(), organization_id=organization_id, step_key=step_key, state="todo")
            .on_conflict_do_nothing(constraint="uq_onboarding_progress_organization_id_step_key")
        )
        stmt = (
            select(OnboardingProgress)
            .where(
                OnboardingProgress.organization_id == organization_id,
                OnboardingProgress.step_key == step_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one()

"""The onboarding checklist (Settings > Onboarding Checklist).

A practice works through a fixed list of steps. A step is finished when the practice's data says
so (counted live, nothing is stored) or when a person marks it done or skips it (the only thing
stored: ``onboarding_progress``). A person's decision wins over the data; taking it back
(``todo``) lets the data speak again. Steps with no data to count (reviewing the VAT codes) can
only be completed by a person.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError
from app.db.models import Organization
from app.db.session import unit_of_work
from app.repositories.onboarding import COUNT_CAP, ManualStep, OnboardingFacts, OnboardingRepository
from app.schemas.onboarding import (
    OnboardingRead,
    OnboardingStepRead,
    OnboardingStepUpdate,
    OnboardingSummary,
    StepSource,
    StepState,
)
from app.services.common import record_audit
from app.services.policy import OrgContext
from app.services.practice_defaults import DEFAULT_INVOICE_TEMPLATE
from app.services.sequences import DEFAULT_SEQUENCES

NUMBERED_KINDS = ("invoice", "credit_note", "estimate")


@dataclass(frozen=True, slots=True)
class Check:
    """What the data says about a step."""

    done: bool
    detail: str | None = None


Detector = Callable[[Organization, OnboardingFacts], Check]


@dataclass(frozen=True, slots=True)
class StepDef:
    key: str
    title: str
    description: str
    link: str
    # None: nothing in the data tells whether the step is finished.
    detect: Detector | None = None


def _count(n: int, singular: str, plural: str | None = None) -> str:
    shown = f"{COUNT_CAP:,}+" if n >= COUNT_CAP else str(n)
    return f"{shown} {singular if n == 1 else plural or singular + 's'}"


def _practice_details(org: Organization, _: OnboardingFacts) -> Check:
    missing = [
        label
        for label, present in (
            ("address", org.address_line1),
            ("postcode", org.postcode),
            ("email or phone", org.email or org.phone),
        )
        if not present
    ]
    return Check(not missing, f"Missing: {', '.join(missing)}" if missing else None)


def _vat_registration(org: Organization, _: OnboardingFacts) -> Check:
    if not org.vat_registered:
        return Check(True, "Not VAT registered")
    if org.vat_number:
        return Check(True, "VAT number recorded")
    return Check(False, "VAT registered, VAT number missing")


def _staff(_: Organization, facts: OnboardingFacts) -> Check:
    # The founder is always there: a second person is the first sign of a team.
    return Check(facts.active_staff >= 2, _count(facts.active_staff, "active staff member"))


def _grades_rates(org: Organization, facts: OnboardingFacts) -> Check:
    done = facts.grades_with_rate > 0 or facts.charge_rules > 0 or org.default_charge_rate > 0
    return Check(done, _count(facts.grades_with_rate, "grade") + " with a default charge-out rate")


def _services(_: Organization, facts: OnboardingFacts) -> Check:
    done = facts.services > 0 and facts.activity_types > 0
    return Check(
        done,
        f"{_count(facts.services, 'active service')}, "
        f"{_count(facts.activity_types, 'active activity type')}",
    )


def _numbering(_: Organization, facts: OnboardingFacts) -> Check:
    return Check(
        facts.numbering_changed,
        None if facts.numbering_changed else "Still the starting numbers and prefixes",
    )


def _template(_: Organization, facts: OnboardingFacts) -> Check:
    return Check(
        facts.templates_customised,
        None if facts.templates_customised else "Still the standard template",
    )


def _clients(_: Organization, facts: OnboardingFacts) -> Check:
    return Check(facts.clients > 0, _count(facts.clients, "client"))


def _jobs(_: Organization, facts: OnboardingFacts) -> Check:
    return Check(facts.jobs > 0, _count(facts.jobs, "job"))


def _first_time(_: Organization, facts: OnboardingFacts) -> Check:
    return Check(facts.has_time_entry, None if facts.has_time_entry else "No time recorded yet")


def _period_lock(_: Organization, facts: OnboardingFacts) -> Check:
    return Check(
        facts.has_period_lock_event, None if facts.has_period_lock_event else "No period locked yet"
    )


def _integrations(_: Organization, facts: OnboardingFacts) -> Check:
    linked = _count(facts.linked_clients, "client")
    return Check(facts.linked_clients > 0, f"{linked} linked to the central service")


# The order is the order a new practice should work in. Links are paths below `/:orgSlug`.
STEPS: tuple[StepDef, ...] = (
    StepDef(
        "practice_details",
        "Practice details",
        "Add the practice's address and contact details: they appear on every invoice.",
        "/settings/practice",
        _practice_details,
    ),
    StepDef(
        "vat_registration",
        "VAT registration",
        "Say whether the practice is VAT registered and record its VAT number.",
        "/settings/practice",
        _vat_registration,
    ),
    StepDef(
        "staff_added",
        "Staff",
        "Add the people who record time, with their grade, working hours and approver.",
        "/team/staff",
        _staff,
    ),
    StepDef(
        "grades_rates",
        "Grades and default rates",
        "Give each grade a default charge-out rate, or add dated rates, so time is priced.",
        "/team/grades",
        _grades_rates,
    ),
    StepDef(
        "services_activity_types",
        "Services and activity types",
        "Review the services the practice sells and the activity types time is recorded against.",
        "/team/services",
        _services,
    ),
    StepDef(
        "vat_codes_reviewed",
        "VAT codes",
        "Check the VAT codes and their rates before the first invoice, then mark this step done.",
        "/settings/vat-codes",
    ),
    StepDef(
        "numbering_configured",
        "Numbering",
        "Check the invoice, credit note and estimate prefixes and the numbers to continue from.",
        "/settings/invoice-numbering",
        _numbering,
    ),
    StepDef(
        "invoice_template",
        "Invoice template",
        "Choose the layout, logo and footer invoices are printed with.",
        "/settings/invoice-templates",
        _template,
    ),
    StepDef(
        "clients_added",
        "Clients",
        "Add clients one by one, or import them from a CSV file.",
        "/clients",
        _clients,
    ),
    StepDef(
        "jobs_created",
        "Jobs",
        "Create the jobs the practice works on, with budgets, dates and assignees.",
        "/jobs",
        _jobs,
    ),
    StepDef(
        "first_time_entry",
        "First time entry",
        "Record the first time entry on a timesheet.",
        "/time",
        _first_time,
    ),
    StepDef(
        "period_lock",
        "Period lock",
        "Lock periods once they are reported, so closed figures cannot change by accident.",
        "/settings/period-lock",
        _period_lock,
    ),
    StepDef(
        "integrations",
        "Integrations",
        "Link clients from the central client service and review the other integrations.",
        "/settings/integrations",
        _integrations,
    ),
)
_BY_KEY = {step.key: step for step in STEPS}


def _compose(
    step: StepDef, org: Organization, facts: OnboardingFacts, manual: ManualStep | None
) -> OnboardingStepRead:
    check = step.detect(org, facts) if step.detect else None
    decided = manual if manual is not None and manual.progress.state != "todo" else None
    state: StepState
    source: StepSource
    if decided is not None:
        state, source = cast(StepState, decided.progress.state), "manual"
    elif check is not None and check.done:
        state, source = "done", "auto"
    else:
        state, source = "todo", "auto" if check is not None else "manual"
    return OnboardingStepRead(
        key=step.key,
        title=step.title,
        description=step.description,
        state=state,
        source=source,
        detected=check.done if check is not None else None,
        detail=check.detail if check is not None else None,
        link=step.link,
        updated_by_name=decided.updated_by_name if decided is not None else None,
        updated_at=decided.progress.updated_at if decided is not None else None,
    )


def _summary(steps: list[OnboardingStepRead]) -> OnboardingSummary:
    done = sum(1 for s in steps if s.state == "done")
    skipped = sum(1 for s in steps if s.state == "skipped")
    todo = len(steps) - done - skipped
    return OnboardingSummary(
        total=len(steps),
        done=done,
        skipped=skipped,
        todo=todo,
        percent=round(100 * (done + skipped) / len(steps)),
        complete=todo == 0,
    )


class OnboardingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = OnboardingRepository(session)

    async def get(self, ctx: OrgContext) -> OnboardingRead:
        org = ctx.organization
        facts = await self.repo.facts(
            org.id,
            default_template_name=DEFAULT_INVOICE_TEMPLATE,
            default_sequences=DEFAULT_SEQUENCES,
            numbered_kinds=NUMBERED_KINDS,
        )
        manual = await self.repo.manual_steps(org.id)
        steps = [_compose(step, org, facts, manual.get(step.key)) for step in STEPS]
        return OnboardingRead(steps=steps, summary=_summary(steps))

    async def set_step(
        self, ctx: OrgContext, step_key: str, data: OnboardingStepUpdate, rctx: RequestContext
    ) -> OnboardingRead:
        step = _BY_KEY.get(step_key)
        if step is None:
            raise NotFoundError("Onboarding step not found.")
        async with unit_of_work(self.session):
            row = await self.repo.lock_step(ctx.organization_id, step.key)
            previous = row.state
            if previous != data.state:
                row.state = data.state
                row.updated_by = ctx.user.id
                await self.session.flush()
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "onboarding.step_updated",
                    "onboarding_step",
                    row.id,
                    {
                        "step_key": step.key,
                        "step_title": step.title,
                        "state": data.state,
                        "changes": {"state": [previous, data.state]},
                    },
                )
        return await self.get(ctx)

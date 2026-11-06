"""Charge-out and cost rate resolution (docs/domain.md, "Rates").

The charge-out rate of a time entry is the first that exists of, most specific first:

1. ``client_service_staff``: a rule for the client, the job's service and the person,
2. ``client_service``: a rule for the client and the job's service,
3. ``client_staff``: a rule for the client and the person, else ``client``: the client's rule
   for everyone,
4. ``job``: ``jobs.hourly_rate`` of the entry's job,
5. ``service_staff``: a rule for the job's service and the person (for every client), else
   ``service_grade``: a rule for the job's service and the person's grade, else
   ``service``: the job's service's rate for everyone,
6. ``staff``: a rule for the person,
7. ``grade``: a rule for the person's grade, else the grade's default charge-out rate,

then ``default``: ``organizations.default_charge_rate`` when it is above 0, else ``none``: 0,
"No rate". A rule counts only on the days inside its effective period.

WIP valuation (APT84): the levels come in four groups, tried in the practice's order
(``organizations.wip_valuation_sources``): ``client`` (levels 1-4, the job's own rate included),
``service`` (5), ``staff`` (6) and ``grade`` (7). A group left out of the order is switched off: it
never prices time, and its steps come back after the others as not applicable, with the reason.
Inside ``service``, ``service_grade`` applies only while ``grade`` is on. The former rate modes are
the orders ``staff_first`` = client, service, staff, grade; ``grade_only`` = client, service, grade;
``staff_only`` = client, service, staff. The cost rate is the person's ``staff`` cost rule, else
the grade's cost rule or default cost rate, else (``staff_first``) the person's first cost rule
when the day is before it starts, else 0, whatever the order. The cost never depends on the
client, the job or how the job is billed.

The entry's service is the service of its job. The person's grade is their current grade.

Overtime never changes these rates (APT81): it is costed apart, in ``time_entry_overtime``
(``app/services/overtime.py``), and only the Staff Cost report shows it.
"""

import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Organization
from app.db.models.organization import WIP_VALUATION_SOURCES
from app.repositories.time_rates import JobRate, RateLookupRepository, RuleKey, RuleRow

CHARGE_SOURCES = (
    "client_service_staff",
    "client_service",
    "client_staff",
    "client",
    "job",
    "service_staff",
    "service_grade",
    "service",
    "staff",
    "grade",
    "default",
    "none",
)
# Level 5 has three sources: the person's own rate for the service, else the grade's, else the
# service's rate for everyone.
STAFF_SERVICE_LABEL = "Staff rate for the service"
SERVICE_LABEL = "Service rate"
LEVEL_LABELS = {
    1: "Client + service + staff",
    2: "Client + service",
    3: "Client + staff",
    4: "Job rate",
    5: "Service rate for the staff member's grade",
    6: "Staff rate",
    7: "Grade rate",
}
OFF_REASON = "Switched off in WIP valuation."
GRADE_OFF_REASON = "Grade rates are switched off in WIP valuation."


def service_sources(sources: Sequence[str]) -> tuple[str, ...]:
    """The sources of level 5 that apply under a WIP valuation order: the grade's rate for the
    service only while grade rates are on."""
    if "grade" in sources:
        return ("service_staff", "service_grade", "service")
    return ("service_staff", "service")


@dataclass(frozen=True, slots=True)
class RateQuery:
    user_id: uuid.UUID
    client_id: uuid.UUID | None
    job_id: uuid.UUID | None
    entry_date: date
    # Overrides the job's service (what-if previews); null = the job's own service.
    service_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ResolvedRate:
    charge_rate: int
    cost_rate: int
    # The level that supplied the charge-out rate (one of ``CHARGE_SOURCES``).
    source: str
    # ``staff``, ``grade``, ``staff_first`` or ``none`` (cost 0).
    cost_source: str = "none"
    charge_rule_id: uuid.UUID | None = None
    cost_rule_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Step:
    """One level of the ladder, evaluated for one query."""

    # 1-7 for the charge-out levels; null for the practice default and for cost steps.
    level: int | None
    source: str
    label: str
    # False when the query lacks what the level needs (a client, a service, a grade, a job).
    applicable: bool
    reason: str | None
    rate: int | None
    # ``rule``, ``job``, ``grade_default`` or ``practice_default``.
    origin: str | None
    rule: RuleRow | None = None

    @property
    def exists(self) -> bool:
        return self.rate is not None


@dataclass(frozen=True, slots=True)
class Explanation:
    """Every step of both ladders and which one won."""

    charge: list[Step]
    charge_selected: int
    cost: list[Step]
    cost_selected: int
    client_id: uuid.UUID | None
    service_id: uuid.UUID | None
    grade_id: uuid.UUID | None
    job: JobRate | None


@dataclass(frozen=True, slots=True)
class _Context:
    user_id: uuid.UUID
    client_id: uuid.UUID | None
    job: JobRate | None
    service_id: uuid.UUID | None
    grade_id: uuid.UUID | None
    day: date


def _step(
    level: int | None,
    source: str,
    label: str,
    *,
    applicable: bool = True,
    reason: str | None = None,
    rule: RuleRow | None = None,
    rate: int | None = None,
    origin: str | None = None,
) -> Step:
    if rule is not None:
        rate, origin = rule.rate, "rule"
    return Step(
        level, source, label, applicable, None if applicable else reason, rate, origin, rule
    )


class RateBook:
    """Everything needed to price many queries, loaded with four queries in total."""

    def __init__(
        self,
        organization: Organization,
        jobs: dict[uuid.UUID, JobRate],
        grades: dict[uuid.UUID, uuid.UUID | None],
        defaults: dict[uuid.UUID, tuple[int | None, int | None]],
        rules: dict[RuleKey, list[RuleRow]],
        sources: Sequence[str] | None = None,
    ) -> None:
        self.default_rate = organization.default_charge_rate
        # The WIP valuation order; ``sources`` overrides the practice's (what-if previews).
        self.sources = tuple(organization.wip_valuation_sources if sources is None else sources)
        self.jobs = jobs
        self.grades = grades
        self.defaults = defaults
        self.rules = rules

    @classmethod
    async def load(
        cls,
        session: AsyncSession,
        organization: Organization,
        queries: Sequence[RateQuery],
        sources: Sequence[str] | None = None,
    ) -> "RateBook":
        repo = RateLookupRepository(session)
        org_id = organization.id
        jobs = await repo.jobs(org_id, {q.job_id for q in queries if q.job_id})
        client_ids = {q.client_id for q in queries if q.client_id} | {
            job.client_id for job in jobs.values()
        }
        user_ids = {q.user_id for q in queries}
        grades = await repo.member_grades(org_id, user_ids)
        grade_ids = {g for g in grades.values() if g is not None}
        defaults = await repo.grade_defaults(org_id, grade_ids)
        service_ids = {q.service_id for q in queries if q.service_id} | {
            job.service_id for job in jobs.values() if job.service_id
        }
        rules = await repo.rules(
            org_id,
            client_ids=client_ids,
            user_ids=user_ids,
            grade_ids=grade_ids,
            service_ids=service_ids,
        )
        return cls(organization, jobs, grades, defaults, rules, sources)

    def context(self, query: RateQuery) -> _Context:
        job = self.jobs.get(query.job_id) if query.job_id else None
        client_id = query.client_id or (job.client_id if job else None)
        service_id = query.service_id or (job.service_id if job else None)
        return _Context(
            query.user_id,
            client_id,
            job,
            service_id,
            self.grades.get(query.user_id),
            query.entry_date,
        )

    def _rule(
        self,
        kind: str,
        client: uuid.UUID | None,
        service: uuid.UUID | None,
        user: uuid.UUID | None,
        grade: uuid.UUID | None,
        day: date,
    ) -> RuleRow | None:
        for row in self.rules.get((kind, client, service, user, grade), ()):
            if row.covers(day):
                return row
        return None

    def charge_steps(self, c: _Context) -> Iterator[Step]:
        """The charge-out levels in order of precedence: the groups switched on in the WIP
        valuation order, then those switched off, then the practice default and "No rate".
        Lazy: stop at the first that exists."""
        groups = {
            "client": self._client_steps,
            "service": self._service_steps,
            "staff": self._staff_steps,
            "grade": self._grade_steps,
        }
        for name in self.sources:
            yield from groups[name](c, True)
        for name in WIP_VALUATION_SOURCES:
            if name not in self.sources:
                yield from groups[name](c, False)
        default = self.default_rate
        yield _step(
            None,
            "default",
            "Practice default rate",
            applicable=default > 0,
            reason="The practice default rate is 0.",
            rate=default if default > 0 else None,
            origin="practice_default" if default > 0 else None,
        )
        yield _step(None, "none", "No rate", rate=0, origin="none")

    def _client_steps(self, c: _Context, on: bool) -> Iterator[Step]:
        """Levels 1-4: the rates agreed with the client, then the job's own rate."""
        client, service, user, day = c.client_id, c.service_id, c.user_id, c.day
        both = on and client is not None and service is not None
        missing = (
            OFF_REASON
            if not on
            else "This time has no client and no service."
            if client is None and service is None
            else "This time has no client."
            if client is None
            else "This job has no service."
        )
        yield _step(
            1,
            "client_service_staff",
            LEVEL_LABELS[1],
            applicable=both,
            reason=missing,
            rule=self._rule("charge", client, service, user, None, day) if both else None,
        )
        yield _step(
            2,
            "client_service",
            LEVEL_LABELS[2],
            applicable=both,
            reason=missing,
            rule=self._rule("charge", client, service, None, None, day) if both else None,
        )
        rule3: RuleRow | None = None
        source3 = "client_staff"
        if on and client is not None:
            rule3 = self._rule("charge", client, None, user, None, day)
            if rule3 is None:
                rule3 = self._rule("charge", client, None, None, None, day)
                if rule3 is not None:
                    source3 = "client"
        yield _step(
            3,
            source3,
            LEVEL_LABELS[3] if source3 == "client_staff" else "Client rate for everyone",
            applicable=on and client is not None,
            reason=OFF_REASON if not on else "This time has no client.",
            rule=rule3,
        )
        job = c.job if on else None
        yield _step(
            4,
            "job",
            LEVEL_LABELS[4],
            applicable=job is not None,
            reason=OFF_REASON if not on else "This time is not on a job.",
            rate=job.hourly_rate if job is not None else None,
            origin="job" if job is not None and job.hourly_rate is not None else None,
        )

    def _service_steps(self, c: _Context, on: bool) -> Iterator[Step]:
        """Level 5: the person's own rate for the service wins over the grade's rate for the
        service (only while grade rates are on), which wins over the service's rate for
        everyone."""
        service, user, grade, day = c.service_id, c.user_id, c.grade_id, c.day
        grade_on = "grade" in self.sources
        rule5: RuleRow | None = None
        source5 = "service_grade"
        if on and service is not None:
            rule5 = self._rule("charge", None, service, user, None, day)
            if rule5 is not None:
                source5 = "service_staff"
            elif grade is not None and grade_on:
                rule5 = self._rule("charge", None, service, None, grade, day)
            if rule5 is None:
                rule5 = self._rule("charge", None, service, None, None, day)
                if rule5 is not None:
                    source5 = "service"
        labels5 = {"service_staff": STAFF_SERVICE_LABEL, "service": SERVICE_LABEL}
        yield _step(
            5,
            source5,
            labels5.get(source5, LEVEL_LABELS[5]),
            applicable=on
            and service is not None
            and (rule5 is not None or (grade is not None and grade_on)),
            reason=OFF_REASON
            if not on
            else "The job has no service."
            if service is None
            else GRADE_OFF_REASON
            if not grade_on
            else "This person has no grade.",
            rule=rule5,
        )

    def _staff_steps(self, c: _Context, on: bool) -> Iterator[Step]:
        """Level 6: the person's own rate."""
        yield _step(
            6,
            "staff",
            LEVEL_LABELS[6],
            applicable=on,
            reason=OFF_REASON,
            rule=self._rule("charge", None, None, c.user_id, None, c.day) if on else None,
        )

    def _grade_steps(self, c: _Context, on: bool) -> Iterator[Step]:
        """Level 7: a rule for the person's grade, else the grade's default charge-out rate."""
        graded = c.grade_id if on else None
        grade_rule = self._rule("charge", None, None, None, graded, c.day) if graded else None
        grade_default = self.defaults.get(graded, (None, None))[0] if graded else None
        yield _step(
            7,
            "grade",
            LEVEL_LABELS[7],
            applicable=graded is not None,
            reason=OFF_REASON if not on else "This person has no grade.",
            rule=grade_rule,
            rate=grade_default if grade_rule is None else None,
            origin="grade_default" if grade_rule is None and grade_default is not None else None,
        )

    def cost_steps(self, c: _Context) -> Iterator[Step]:
        own = self.rules.get(("cost", None, None, c.user_id, None), ())
        yield _step(
            None,
            "staff",
            "Staff cost rate",
            rule=self._rule("cost", None, None, c.user_id, None, c.day),
        )
        grade = c.grade_id
        grade_rule = self._rule("cost", None, None, None, grade, c.day) if grade else None
        grade_default = self.defaults.get(grade, (None, None))[1] if grade else None
        yield _step(
            None,
            "grade",
            "Grade cost rate",
            applicable=grade is not None,
            reason="This person has no grade.",
            rule=grade_rule,
            rate=grade_default if grade_rule is None else None,
            origin="grade_default" if grade_rule is None and grade_default is not None else None,
        )
        # Time dated before the person's first cost rate starts is costed at that first rate
        # (rules are oldest first): a rate added "from today" still costs the time already in.
        first = own[0] if own and c.day < own[0].effective_from else None
        yield _step(
            None,
            "staff_first",
            "Staff's first cost rate (time before it starts)",
            applicable=bool(own),
            reason="This person has no cost rate of their own.",
            rule=first,
        )
        yield _step(None, "none", "No cost rate (zero)", rate=0, origin="none")

    def resolve(self, query: RateQuery) -> ResolvedRate:
        c = self.context(query)
        charge = next(s for s in self.charge_steps(c) if s.exists)
        cost = next(s for s in self.cost_steps(c) if s.exists)
        assert charge.rate is not None and cost.rate is not None
        return ResolvedRate(
            charge.rate,
            cost.rate,
            charge.source,
            cost.source,
            charge.rule.id if charge.rule else None,
            cost.rule.id if cost.rule else None,
        )

    def tiers(self, query: RateQuery) -> dict[str, int | None]:
        """The first rate of each of the three tiers of the charge-out ladder, for the recovery
        analysis: ``client_job`` (levels 1-4: agreed with the client, else the job's own rate),
        ``task`` (level 5: set against the job's service) and ``staff`` (levels 6, 7 and the
        practice default: the person's charge-out rate; always found). Null when the tier has
        none."""
        found: dict[str, int | None] = {"client_job": None, "task": None, "staff": None}
        for step in self.charge_steps(self.context(query)):
            if step.rate is None:
                continue
            level = step.level
            tier = (
                "client_job"
                if level is not None and level <= 4
                else "task"
                if level == 5
                else "staff"
            )
            if found[tier] is None:
                found[tier] = step.rate
        return found

    def explain(self, query: RateQuery) -> Explanation:
        c = self.context(query)
        charge = list(self.charge_steps(c))
        cost = list(self.cost_steps(c))
        return Explanation(
            charge=charge,
            charge_selected=next(i for i, s in enumerate(charge) if s.exists),
            cost=cost,
            cost_selected=next(i for i, s in enumerate(cost) if s.exists),
            client_id=c.client_id,
            service_id=c.service_id,
            grade_id=c.grade_id,
            job=c.job,
        )


async def resolve_many(
    session: AsyncSession,
    organization: Organization,
    queries: Sequence[RateQuery],
    sources: Sequence[str] | None = None,
) -> list[ResolvedRate]:
    """Resolve many entries with four queries in total, in the order given. ``sources``: a WIP
    valuation order to price by instead of the practice's (a preview)."""
    if not queries:
        return []
    book = await RateBook.load(session, organization, queries, sources)
    return [book.resolve(q) for q in queries]


async def resolve(
    session: AsyncSession,
    organization: Organization,
    user_id: uuid.UUID,
    client_id: uuid.UUID | None,
    job_id: uuid.UUID | None,
    entry_date: date,
) -> tuple[int, int]:
    """``(charge_rate, cost_rate)`` for one entry, in minor units per hour."""
    [found] = await resolve_many(
        session, organization, [RateQuery(user_id, client_id, job_id, entry_date)]
    )
    return found.charge_rate, found.cost_rate


async def explain(
    session: AsyncSession, organization: Organization, query: RateQuery
) -> Explanation:
    """Both ladders for one query, with the level that wins."""
    book = await RateBook.load(session, organization, [query])
    return book.explain(query)

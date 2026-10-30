"""Charge-out and cost rate resolution (docs/domain.md, "Rates").

The charge-out rate of a time entry is the first that exists of, most specific first:

1. ``client_service_staff``: a rule for the client, the job's service and the person,
2. ``client_service``: a rule for the client and the job's service,
3. ``client_staff``: a rule for the client and the person, else ``client``: the client's rule
   for everyone,
4. ``job``: ``jobs.hourly_rate`` of the entry's job,
5. ``service_staff``: a rule for the job's service and the person (for every client), else
   ``service_grade``: a rule for the job's service and the person's grade,
6. ``staff``: a rule for the person,
7. ``grade``: a rule for the person's grade, else the grade's default charge-out rate,

and finally ``default``: ``organizations.default_charge_rate``. A rule counts only on the days
inside its effective period. In the practice's ``grade_only`` rate mode level 6 is skipped, so a
person's own rate never prices time and the grade's rate does (levels 1-5 are unchanged). The
cost rate is the person's ``staff`` cost rule, else the grade's cost rule or default cost rate,
else (``staff_first``) the person's first cost rule when the day is before it starts, else 0,
in either mode. The cost never depends on the client, the job or how the job is billed.

The entry's service is the service of its job. The person's grade is their current grade.

Overtime: in a practice that has it on, a query marked ``overtime`` is priced at the rates above
times the person's own multiplier, else the practice's (basis points, 15000 = 1.5x), rounded half
up, for the charge-out and the cost rate alike. Everything else is priced as before.
"""

import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Organization
from app.repositories.time_rates import JobRate, RateLookupRepository, RuleKey, RuleRow

CHARGE_SOURCES = (
    "client_service_staff",
    "client_service",
    "client_staff",
    "client",
    "job",
    "service_staff",
    "service_grade",
    "staff",
    "grade",
    "default",
)
# Level 5 has two sources: the person's own rate for the service, else the grade's.
STAFF_SERVICE_LABEL = "Staff rate for the service"
LEVEL_LABELS = {
    1: "Client + service + staff",
    2: "Client + service",
    3: "Client + staff",
    4: "Job rate",
    5: "Service rate for the staff member's grade",
    6: "Staff rate",
    7: "Grade rate",
}


@dataclass(frozen=True, slots=True)
class RateQuery:
    user_id: uuid.UUID
    client_id: uuid.UUID | None
    job_id: uuid.UUID | None
    entry_date: date
    # Overrides the job's service (what-if previews); null = the job's own service.
    service_id: uuid.UUID | None = None
    # Overtime time; ignored unless the practice has overtime on.
    overtime: bool = False


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


def _times_bp(rate: int, bp: int) -> int:
    """``rate`` x ``bp`` / 10000, rounded half up."""
    return (rate * bp + 5000) // 10000


class RateBook:
    """Everything needed to price many queries, loaded with four queries in total."""

    def __init__(
        self,
        organization: Organization,
        jobs: dict[uuid.UUID, JobRate],
        grades: dict[uuid.UUID, uuid.UUID | None],
        defaults: dict[uuid.UUID, tuple[int | None, int | None]],
        rules: dict[RuleKey, list[RuleRow]],
        overtime: dict[uuid.UUID, int | None] | None = None,
    ) -> None:
        self.default_rate = organization.default_charge_rate
        self.overtime_enabled = organization.overtime_enabled
        self.overtime_default_bp = organization.overtime_multiplier_bp
        self.overtime = overtime or {}
        self.rate_mode = organization.rate_mode
        self.jobs = jobs
        self.grades = grades
        self.defaults = defaults
        self.rules = rules

    @classmethod
    async def load(
        cls, session: AsyncSession, organization: Organization, queries: Sequence[RateQuery]
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
        rules = await repo.rules(
            org_id, client_ids=client_ids, user_ids=user_ids, grade_ids=grade_ids
        )
        overtime = None
        if organization.overtime_enabled and any(q.overtime for q in queries):
            overtime = await repo.member_overtime(
                org_id, {q.user_id for q in queries if q.overtime}
            )
        return cls(organization, jobs, grades, defaults, rules, overtime)

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
        """The charge-out levels in order of precedence. Lazy: stop at the first that exists."""
        client, service, user, grade, day = c.client_id, c.service_id, c.user_id, c.grade_id, c.day
        both = client is not None and service is not None
        missing = (
            "This time has no client and no service."
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
        if client is not None:
            rule3 = self._rule("charge", client, None, user, None, day)
            if rule3 is None:
                rule3 = self._rule("charge", client, None, None, None, day)
                if rule3 is not None:
                    source3 = "client"
        yield _step(
            3,
            source3,
            LEVEL_LABELS[3] if source3 == "client_staff" else "Client rate for everyone",
            applicable=client is not None,
            reason="This time has no client.",
            rule=rule3,
        )
        job = c.job
        yield _step(
            4,
            "job",
            LEVEL_LABELS[4],
            applicable=job is not None,
            reason="This time is not on a job.",
            rate=job.hourly_rate if job is not None else None,
            origin="job" if job is not None and job.hourly_rate is not None else None,
        )
        # The person's own rate for the service wins over the grade's rate for the service.
        rule5: RuleRow | None = None
        source5 = "service_grade"
        if service is not None:
            rule5 = self._rule("charge", None, service, user, None, day)
            if rule5 is not None:
                source5 = "service_staff"
            elif grade is not None:
                rule5 = self._rule("charge", None, service, None, grade, day)
        yield _step(
            5,
            source5,
            STAFF_SERVICE_LABEL if source5 == "service_staff" else LEVEL_LABELS[5],
            applicable=service is not None and (grade is not None or rule5 is not None),
            reason="The job has no service." if service is None else "This person has no grade.",
            rule=rule5,
        )
        grade_only = self.rate_mode == "grade_only"
        yield _step(
            6,
            "staff",
            LEVEL_LABELS[6],
            applicable=not grade_only,
            reason="This practice prices by grade only: staff rates are not used.",
            rule=None if grade_only else self._rule("charge", None, None, user, None, day),
        )
        grade_rule = self._rule("charge", None, None, None, grade, day) if grade else None
        grade_default = self.defaults.get(grade, (None, None))[0] if grade else None
        yield _step(
            7,
            "grade",
            LEVEL_LABELS[7],
            applicable=grade is not None,
            reason="This person has no grade.",
            rule=grade_rule,
            rate=grade_default if grade_rule is None else None,
            origin="grade_default" if grade_rule is None and grade_default is not None else None,
        )
        yield _step(
            None,
            "default",
            "Practice default rate",
            rate=self.default_rate,
            origin="practice_default",
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
        charge_rate, cost_rate = charge.rate, cost.rate
        if query.overtime and self.overtime_enabled:
            bp = self.overtime.get(query.user_id) or self.overtime_default_bp
            charge_rate = _times_bp(charge_rate, bp)
            cost_rate = _times_bp(cost_rate, bp)
        return ResolvedRate(
            charge_rate,
            cost_rate,
            charge.source,
            cost.source,
            charge.rule.id if charge.rule else None,
            cost.rule.id if cost.rule else None,
        )

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
    session: AsyncSession, organization: Organization, queries: Sequence[RateQuery]
) -> list[ResolvedRate]:
    """Resolve many entries with four queries in total, in the order given."""
    if not queries:
        return []
    book = await RateBook.load(session, organization, queries)
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

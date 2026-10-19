"""Request and response models of dated rate rules, rate resolution, rate history and re-rating."""

import uuid
from datetime import date, datetime
from typing import Literal, Self, cast

from pydantic import BaseModel, Field, model_validator

from app.db.models import RateChange, RateRerun, RateRerunItem
from app.repositories.rate_history import ChangeView
from app.repositories.rate_rules import RuleView
from app.schemas.common import RequestModel
from app.schemas.settings import Money, reject_nulls
from app.services.charge_rates import Explanation, Step

RateKind = Literal["charge", "cost"]
RateLevel = Literal[
    "client_service_staff",
    "client_service",
    "client_staff",
    "client",
    "service_grade",
    "staff",
    "grade",
]
RuleSort = Literal["effective_from", "rate", "created_at"]
RuleStatus = Literal["current", "future", "ended"]
ChangeAction = Literal["created", "updated", "end_dated", "deleted"]

# Precedence of a rule level in the charge-out ladder (the job rate is 4).
PRECEDENCE: dict[str, int] = {
    "client_service_staff": 1,
    "client_service": 2,
    "client_staff": 3,
    "client": 3,
    "service_grade": 5,
    "staff": 6,
    "grade": 7,
}
LEVEL_LABELS: dict[str, str] = {
    "client_service_staff": "Client + service + staff",
    "client_service": "Client + service",
    "client_staff": "Client + staff",
    "client": "Client (everyone)",
    "service_grade": "Service + grade",
    "staff": "Staff",
    "grade": "Grade",
}


class RefRead(BaseModel):
    id: uuid.UUID
    code: str | None
    name: str


class StaffRef(BaseModel):
    id: uuid.UUID
    code: str | None
    full_name: str


def _ref(id_: uuid.UUID | None, code: str | None, name: str | None) -> RefRead | None:
    return RefRead(id=id_, code=code, name=name) if id_ is not None and name is not None else None


def _staff(id_: uuid.UUID | None, code: str | None, name: str | None) -> StaffRef | None:
    return StaffRef(id=id_, code=code, full_name=name) if id_ is not None and name else None


def rule_status(effective_from: date, effective_to: date | None, today: date) -> RuleStatus:
    if effective_from > today:
        return "future"
    if effective_to is not None and effective_to < today:
        return "ended"
    return "current"


class RateRuleRead(BaseModel):
    id: uuid.UUID
    kind: RateKind
    level: RateLevel
    level_label: str
    # Position in the charge-out ladder (1 = most specific); the job rate is 4.
    precedence: int
    client: RefRead | None
    service: RefRead | None
    staff: StaffRef | None
    grade: RefRead | None
    # Pence per hour. Null for a cost rate when the caller lacks ``rate.read``.
    rate: int | None
    effective_from: date
    effective_to: date | None
    status: RuleStatus
    # True once time falls inside the rule's scope and period: it can be end-dated, not deleted.
    is_used: bool
    can_delete: bool
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(cls, view: RuleView, *, today: date, show_cost: bool) -> "RateRuleRead":
        rule = view.rule
        hidden = rule.kind == "cost" and not show_cost
        return cls(
            id=rule.id,
            kind=cast(RateKind, rule.kind),
            level=cast(RateLevel, rule.level),
            level_label=LEVEL_LABELS[rule.level],
            precedence=PRECEDENCE[rule.level],
            client=_ref(rule.client_id, view.client_code, view.client_name),
            service=_ref(rule.service_id, view.service_code, view.service_name),
            staff=_staff(rule.user_id, view.staff_code, view.user_name),
            grade=_ref(rule.grade_id, view.grade_code, view.grade_name),
            rate=None if hidden else rule.rate,
            effective_from=rule.effective_from,
            effective_to=rule.effective_to,
            status=rule_status(rule.effective_from, rule.effective_to, today),
            is_used=view.is_used,
            can_delete=not view.is_used,
            created_by=rule.created_by,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )


class RateRuleCreate(RequestModel):
    kind: RateKind
    # Which of these four are set decides the level: client + service + user = client_service_staff,
    # client + service, client + user, client alone (everyone), service + grade, user, grade.
    client_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = Field(default=None, description="The staff member's id")
    grade_id: uuid.UUID | None = None
    rate: Money = Field(description="Pence per hour")
    effective_from: date
    effective_to: date | None = Field(default=None, description="Inclusive; null = open-ended")
    close_previous: bool = Field(
        default=False,
        description=(
            "End-date the open-ended rule of the same scope the day before `effective_from` "
            "instead of answering 409 RATE_OVERLAP"
        ),
    )

    @model_validator(mode="after")
    def _period(self) -> Self:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to: Must not be before effective_from")
        return self


class RateRuleUpdate(RequestModel):
    """Scope and kind never change. A rule that has been used keeps its start date."""

    rate: Money | None = None
    effective_from: date | None = None
    # Send null to make the rule open-ended again.
    effective_to: date | None = None

    @model_validator(mode="after")
    def _valid(self) -> Self:
        reject_nulls(self, "rate", "effective_from")
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("effective_to: Must not be before effective_from")
        return self


# --- Resolution -------------------------------------------------------------------------


class LadderRule(BaseModel):
    id: uuid.UUID
    effective_from: date
    effective_to: date | None


class LadderStep(BaseModel):
    # 1-7 for the charge-out levels; null for the practice default and for cost steps.
    level: int | None
    source: str
    label: str
    # False when the query lacks what the level needs (see `reason`).
    applicable: bool
    reason: str | None
    exists: bool
    rate: int | None
    # rule | job | grade_default | practice_default | none
    origin: str | None
    rule: LadderRule | None
    selected: bool

    @classmethod
    def from_step(cls, step: Step, *, selected: bool, show_cost: bool, cost: bool) -> "LadderStep":
        hide = cost and not show_cost
        return cls(
            level=step.level,
            source=step.source,
            label=step.label,
            applicable=step.applicable,
            reason=step.reason,
            exists=step.exists,
            rate=None if hide else step.rate,
            origin=step.origin,
            rule=LadderRule(
                id=step.rule.id,
                effective_from=step.rule.effective_from,
                effective_to=step.rule.effective_to,
            )
            if step.rule
            else None,
            selected=selected,
        )


class ResolveRead(BaseModel):
    on_date: date
    staff: StaffRef
    client: RefRead | None
    service: RefRead | None
    job_id: uuid.UUID | None
    grade: RefRead | None
    charge_rate: int
    charge_source: str
    charge_rule_id: uuid.UUID | None
    cost_rate: int | None
    cost_source: str
    cost_rule_id: uuid.UUID | None
    charge_ladder: list[LadderStep]
    cost_ladder: list[LadderStep]

    @classmethod
    def build(
        cls,
        *,
        day: date,
        staff: StaffRef,
        client: RefRead | None,
        service: RefRead | None,
        job_id: uuid.UUID | None,
        grade: RefRead | None,
        explanation: Explanation,
        show_cost: bool,
    ) -> "ResolveRead":
        charge = explanation.charge[explanation.charge_selected]
        cost = explanation.cost[explanation.cost_selected]
        assert charge.rate is not None and cost.rate is not None
        return cls(
            on_date=day,
            staff=staff,
            client=client,
            service=service,
            job_id=job_id,
            grade=grade,
            charge_rate=charge.rate,
            charge_source=charge.source,
            charge_rule_id=charge.rule.id if charge.rule else None,
            cost_rate=cost.rate if show_cost else None,
            cost_source=cost.source,
            cost_rule_id=cost.rule.id if cost.rule else None,
            charge_ladder=[
                LadderStep.from_step(
                    s, selected=i == explanation.charge_selected, show_cost=show_cost, cost=False
                )
                for i, s in enumerate(explanation.charge)
            ],
            cost_ladder=[
                LadderStep.from_step(
                    s, selected=i == explanation.cost_selected, show_cost=show_cost, cost=True
                )
                for i, s in enumerate(explanation.cost)
            ],
        )


# --- History ----------------------------------------------------------------------------


class RateChangeRead(BaseModel):
    id: uuid.UUID
    at: datetime
    action: ChangeAction
    kind: RateKind
    level: str
    level_label: str
    rule_id: uuid.UUID | None
    client: RefRead | None
    service: RefRead | None
    staff: StaffRef | None
    grade: RefRead | None
    # {"rate", "effective_from", "effective_to"}; null on create (old) and delete (new). For a
    # grade default: {"rate", "default": true}.
    old: dict[str, object] | None
    new: dict[str, object] | None
    actor_id: uuid.UUID | None
    actor_name: str | None
    source: str

    @classmethod
    def from_view(cls, view: ChangeView, *, show_cost: bool) -> "RateChangeRead":
        change: RateChange = view.change
        hide = change.kind == "cost" and not show_cost

        def mask(values: dict[str, object] | None) -> dict[str, object] | None:
            if values is None or not hide:
                return values
            return {**values, "rate": None}

        return cls(
            id=change.id,
            at=change.created_at,
            action=cast(ChangeAction, change.action),
            kind=cast(RateKind, change.kind),
            level=change.level,
            level_label=LEVEL_LABELS[change.level],
            rule_id=change.rule_id,
            client=_ref(change.client_id, None, view.client_name),
            service=_ref(change.service_id, None, view.service_name),
            staff=_staff(change.user_id, None, view.user_name),
            grade=_ref(change.grade_id, None, view.grade_name),
            old=mask(change.old),
            new=mask(change.new),
            actor_id=change.actor_user_id,
            actor_name=view.actor_name,
            source=change.source,
        )


# --- Re-rating --------------------------------------------------------------------------


class RerateScope(RequestModel):
    """Which unbilled time to re-rate. Name clients, jobs and/or a date range; `user_ids`
    narrows further."""

    client_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    job_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ValueError("date_to: Must not be before date_from")
        has_range = self.date_from is not None and self.date_to is not None
        if not (self.client_ids or self.job_ids or has_range):
            raise ValueError(
                "scope: Choose clients, jobs or a date range (both date_from and date_to)"
            )
        return self


class RerateRequest(RequestModel):
    scope: RerateScope
    item_limit: int = Field(
        default=500, ge=0, le=5000, description="How many entries to list (totals cover all)"
    )
    include_unchanged: bool = Field(
        default=False, description="Also list entries whose rate would not change"
    )


class RerateApply(RequestModel):
    scope: RerateScope
    preview_token: str = Field(min_length=10, max_length=2000)
    note: str | None = Field(default=None, max_length=500)


class RerateValues(BaseModel):
    charge_rate: int
    charge_value: int
    cost_rate: int | None
    cost_value: int | None
    source: str | None


class RerateEntry(BaseModel):
    entry_id: uuid.UUID
    entry_date: date
    minutes: int
    staff: StaffRef | None
    client: RefRead | None
    job: RefRead | None
    # change | unchanged | skipped
    outcome: str
    skip_reason: str | None
    old: RerateValues
    new: RerateValues


class RerateTotals(BaseModel):
    entries_examined: int
    entries_to_change: int
    entries_unchanged: int
    entries_skipped: int
    charge_value_before: int
    charge_value_after: int
    charge_value_delta: int
    cost_value_before: int | None
    cost_value_after: int | None
    cost_value_delta: int | None


class RerateSkipped(BaseModel):
    """Entries in scope that are left alone, by reason."""

    billed: int
    in_draft: int
    written_off: int
    period_locked: int


class ReratePreview(BaseModel):
    preview_token: str
    expires_at: datetime
    scope: RerateScope
    totals: RerateTotals
    skipped: RerateSkipped
    items: list[RerateEntry]
    items_truncated: bool


class RerunRead(BaseModel):
    id: uuid.UUID
    created_at: datetime
    source: str
    scope: dict[str, object]
    entries_examined: int
    entries_changed: int
    entries_skipped: int
    charge_value_before: int
    charge_value_after: int
    charge_value_delta: int
    cost_value_before: int | None
    cost_value_after: int | None
    cost_value_delta: int | None
    requested_by: uuid.UUID | None
    requested_by_name: str | None
    note: str | None

    @classmethod
    def from_model(
        cls, run: RateRerun, requested_by_name: str | None, *, show_cost: bool
    ) -> "RerunRead":
        return cls(
            id=run.id,
            created_at=run.created_at,
            source=run.source,
            scope=run.scope,
            entries_examined=run.entries_examined,
            entries_changed=run.entries_changed,
            entries_skipped=run.entries_skipped,
            charge_value_before=run.charge_value_before,
            charge_value_after=run.charge_value_after,
            charge_value_delta=run.charge_value_after - run.charge_value_before,
            cost_value_before=run.cost_value_before if show_cost else None,
            cost_value_after=run.cost_value_after if show_cost else None,
            cost_value_delta=run.cost_value_after - run.cost_value_before if show_cost else None,
            requested_by=run.requested_by,
            requested_by_name=requested_by_name,
            note=run.note,
        )


class RerunItemRead(BaseModel):
    id: uuid.UUID
    entry_id: uuid.UUID
    entry_date: date
    minutes: int
    staff: StaffRef | None
    client: RefRead | None
    job: RefRead | None
    outcome: str
    skip_reason: str | None
    old: RerateValues
    new: RerateValues

    @classmethod
    def from_model(
        cls,
        item: RateRerunItem,
        *,
        staff: StaffRef | None,
        client: RefRead | None,
        job: RefRead | None,
        show_cost: bool,
    ) -> "RerunItemRead":
        return cls(
            id=item.id,
            entry_id=item.time_entry_id,
            entry_date=item.entry_date,
            minutes=item.minutes,
            staff=staff,
            client=client,
            job=job,
            outcome=item.outcome,
            skip_reason=item.skip_reason,
            old=RerateValues(
                charge_rate=item.old_charge_rate,
                charge_value=item.old_charge_value,
                cost_rate=item.old_cost_rate if show_cost else None,
                cost_value=item.old_cost_value if show_cost else None,
                source=item.old_source,
            ),
            new=RerateValues(
                charge_rate=item.new_charge_rate,
                charge_value=item.new_charge_value,
                cost_rate=item.new_cost_rate if show_cost else None,
                cost_value=item.new_cost_value if show_cost else None,
                source=item.new_source,
            ),
        )

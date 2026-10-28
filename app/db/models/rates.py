"""Rates: dated rate rules, their change history and re-rate runs.

One ``rate_rules`` table holds every charge-out and cost rate that is not a job's own rate.
What a rule applies to is set by which of ``client_id``, ``service_id``, ``user_id`` and
``grade_id`` are filled (null = "any"); the combination is the rule's ``level``:

* ``client_service_staff``: client, service and user
* ``client_service``: client and service
* ``client_staff``: client and user
* ``client``: client alone (everyone working for the client)
* ``service_staff``: service and user (a person's own rate for one service, for every client)
* ``service_grade``: service and grade
* ``staff``: user alone
* ``grade``: grade alone

Cost rules exist only for the ``staff`` and ``grade`` levels. A rule is in force from
``effective_from`` to ``effective_to`` inclusive (open-ended when null); the rules of one
scope never overlap (an exclusion constraint on the date range). Time entries snapshot the
rate when they are saved, so a rule never rewrites recorded time.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin
from app.db.models.clients import _member_fk

RATE_KINDS = ("charge", "cost")
RATE_LEVELS = (
    "client_service_staff",
    "client_service",
    "client_staff",
    "client",
    "service_staff",
    "service_grade",
    "staff",
    "grade",
)
# Where a time entry's charge-out rate came from (``time_entries.rate_source``).
RATE_SOURCES = (
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
_NIL = "'00000000-0000-0000-0000-000000000000'::uuid"


def _scope_check() -> str:
    def clause(level: str, client: bool, service: bool, user: bool, grade: bool) -> str:
        columns = {
            "client_id": client,
            "service_id": service,
            "user_id": user,
            "grade_id": grade,
        }
        parts = [f"level = '{level}'"] + [
            f"{name} IS {'NOT NULL' if filled else 'NULL'}" for name, filled in columns.items()
        ]
        return "(" + " AND ".join(parts) + ")"

    return " OR ".join(
        [
            clause("client_service_staff", True, True, True, False),
            clause("client_service", True, True, False, False),
            clause("client_staff", True, False, True, False),
            clause("client", True, False, False, False),
            clause("service_staff", False, True, True, False),
            clause("service_grade", False, True, False, True),
            clause("staff", False, False, True, False),
            clause("grade", False, False, False, True),
        ]
    )


class RateRule(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "rate_rules"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_rate_rules_organization_id_id"),
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_rate_rules_organization_id_client_id_clients",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_rate_rules_organization_id_service_id_services",
        ),
        _member_fk("user_id", "rate_rules"),
        ForeignKeyConstraint(
            ["organization_id", "grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_rate_rules_organization_id_grade_id_grades",
        ),
        CheckConstraint("kind IN ('charge', 'cost')", name="kind"),
        CheckConstraint(
            "level IN ('client_service_staff', 'client_service', 'client_staff', 'client', "
            "'service_staff', 'service_grade', 'staff', 'grade')",
            name="level",
        ),
        CheckConstraint(_scope_check(), name="scope"),
        CheckConstraint("kind = 'charge' OR level IN ('staff', 'grade')", name="cost_level"),
        CheckConstraint("rate >= 0", name="rate"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="period"),
        # Concurrency-safe: two rules of one scope can never cover the same day.
        ExcludeConstraint(
            ("organization_id", "="),
            ("kind", "="),
            (text(f"coalesce(client_id, {_NIL})"), "="),
            (text(f"coalesce(service_id, {_NIL})"), "="),
            (text(f"coalesce(user_id, {_NIL})"), "="),
            (text(f"coalesce(grade_id, {_NIL})"), "="),
            (text("daterange(effective_from, effective_to, '[]')"), "&&"),
            name="ex_rate_rules_scope_period",
            using="gist",
        ),
        Index("ix_rate_rules_organization_id_user_id", "organization_id", "user_id"),
        Index("ix_rate_rules_organization_id_client_id", "organization_id", "client_id"),
        Index("ix_rate_rules_organization_id_grade_id", "organization_id", "grade_id"),
        Index("ix_rate_rules_organization_id_service_id", "organization_id", "service_id"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    grade_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Pence per hour.
    rate: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class RateChange(Base, IdMixin, OrgScopedMixin):
    """Append-only history of every change to a rate: who, when, old and new values.

    ``rule_id`` has no foreign key: the history outlives a deleted rule. A change of a grade's
    default rate has no rule (``rule_id`` null, ``source`` = ``grade_default``).
    """

    __tablename__ = "rate_changes"
    __table_args__ = (
        CheckConstraint("action IN ('created', 'updated', 'end_dated', 'deleted')", name="action"),
        CheckConstraint("kind IN ('charge', 'cost')", name="kind"),
        Index("ix_rate_changes_organization_id_created_at", "organization_id", "created_at"),
        Index(
            "ix_rate_changes_organization_id_user_id_created_at",
            "organization_id",
            "user_id",
            "created_at",
        ),
        Index(
            "ix_rate_changes_organization_id_grade_id_created_at",
            "organization_id",
            "grade_id",
            "created_at",
        ),
        Index(
            "ix_rate_changes_organization_id_client_id_created_at",
            "organization_id",
            "client_id",
            "created_at",
        ),
        Index("ix_rate_changes_rule_id", "rule_id"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    grade_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    old: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    new: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Which door the change came through: ``rates`` (the V2 endpoints), ``staff_rates`` /
    # ``client_rates`` (the V1 endpoints) or ``grade_default``.
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="rates")


class RateRerun(Base, IdMixin, OrgScopedMixin):
    """One application of re-rating to unbilled time (``rate_rerun_items`` hold the entries)."""

    __tablename__ = "rate_reruns"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_rate_reruns_organization_id_id"),
        CheckConstraint("source IN ('rerate', 'revalue')", name="source"),
        Index("ix_rate_reruns_organization_id_created_at", "organization_id", "created_at"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # ``rerate`` = previewed and applied; ``revalue`` = the V1 endpoint (no preview).
    source: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    scope_hash: Mapped[str] = mapped_column(Text, nullable=False)
    entries_examined: Mapped[int] = mapped_column(Integer, nullable=False)
    entries_changed: Mapped[int] = mapped_column(Integer, nullable=False)
    entries_skipped: Mapped[int] = mapped_column(Integer, nullable=False)
    # Why entries in scope were left alone: {"billed": n, "in_draft": n, ...}.
    skipped: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    charge_value_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charge_value_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_value_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_value_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    note: Mapped[str | None] = mapped_column(Text)


class RateRerunItem(Base, IdMixin, OrgScopedMixin):
    """The before and after of one time entry in a re-rate run (changed or skipped)."""

    __tablename__ = "rate_rerun_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["rate_reruns.organization_id", "rate_reruns.id"],
            name="fk_rate_rerun_items_organization_id_run_id_rate_reruns",
            ondelete="CASCADE",
        ),
        CheckConstraint("outcome IN ('changed', 'skipped')", name="outcome"),
        Index("ix_rate_rerun_items_organization_id_run_id", "organization_id", "run_id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # No foreign key: an unbilled entry may be deleted later; the run keeps its record.
    time_entry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(Text)
    old_charge_rate: Mapped[int] = mapped_column(BigInteger, nullable=False)
    new_charge_rate: Mapped[int] = mapped_column(BigInteger, nullable=False)
    old_charge_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    new_charge_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    old_cost_rate: Mapped[int] = mapped_column(BigInteger, nullable=False)
    new_cost_rate: Mapped[int] = mapped_column(BigInteger, nullable=False)
    old_cost_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    new_cost_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    old_source: Mapped[str | None] = mapped_column(Text)
    new_source: Mapped[str | None] = mapped_column(Text)


__all__ = [
    "RATE_KINDS",
    "RATE_LEVELS",
    "RATE_SOURCES",
    "RateChange",
    "RateRerun",
    "RateRerunItem",
    "RateRule",
]

"""rates: dated rate rules, change history and re-rate runs

Replaces ``staff_rates`` and ``client_rates`` with one ``rate_rules`` table that covers every
charge-out and cost level (client + service + staff, client + service, client + staff, client,
service + grade, staff, grade), each with an effective period that cannot overlap another rule
of the same scope (``btree_gist`` exclusion constraint). Adds ``rate_changes`` (history),
``rate_reruns`` / ``rate_rerun_items`` (re-rating) and ``time_entries.rate_source``.

V1 data is carried over: every staff rate row becomes a charge rule and (when the cost is not
zero) a cost rule, each ending the day before the person's next row; every client rate becomes
an open-ended rule from 2000-01-01 (V1 client rates had no dates).

Revision ID: 011
Revises: 010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NIL = "'00000000-0000-0000-0000-000000000000'::uuid"
SOURCES = (
    "'client_service_staff', 'client_service', 'client_staff', 'client', 'job', "
    "'service_grade', 'staff', 'grade', 'default'"
)


def _scope_check() -> str:
    def clause(level: str, client: bool, service: bool, user: bool, grade: bool) -> str:
        columns = {"client_id": client, "service_id": service, "user_id": user, "grade_id": grade}
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
            clause("service_grade", False, True, False, True),
            clause("staff", False, False, True, False),
            clause("grade", False, False, False, True),
        ]
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "rate_rules",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=True),
        sa.Column("service_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("grade_id", sa.UUID(), nullable=True),
        sa.Column("rate", sa.BigInteger(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("kind IN ('charge', 'cost')", name=op.f("ck_rate_rules_kind")),
        sa.CheckConstraint(
            "level IN ('client_service_staff', 'client_service', 'client_staff', 'client', "
            "'service_grade', 'staff', 'grade')",
            name=op.f("ck_rate_rules_level"),
        ),
        sa.CheckConstraint(_scope_check(), name=op.f("ck_rate_rules_scope")),
        sa.CheckConstraint(
            "kind = 'charge' OR level IN ('staff', 'grade')", name=op.f("ck_rate_rules_cost_level")
        ),
        sa.CheckConstraint("rate >= 0", name=op.f("ck_rate_rules_rate")),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_rate_rules_period"),
        ),
        postgresql.ExcludeConstraint(
            ("organization_id", "="),
            ("kind", "="),
            (sa.text(f"coalesce(client_id, {NIL})"), "="),
            (sa.text(f"coalesce(service_id, {NIL})"), "="),
            (sa.text(f"coalesce(user_id, {NIL})"), "="),
            (sa.text(f"coalesce(grade_id, {NIL})"), "="),
            (sa.text("daterange(effective_from, effective_to, '[]')"), "&&"),
            name="ex_rate_rules_scope_period",
            using="gist",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_rate_rules_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_rate_rules_organization_id_client_id_clients",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_rate_rules_organization_id_grade_id_grades",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_rate_rules_organization_id_service_id_services",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_rate_rules_organization_id_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_rate_rules_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_rules")),
        sa.UniqueConstraint("organization_id", "id", name="uq_rate_rules_organization_id_id"),
    )
    for column in ("user_id", "client_id", "grade_id", "service_id"):
        op.create_index(
            f"ix_rate_rules_organization_id_{column}",
            "rate_rules",
            ["organization_id", column],
            unique=False,
        )
    add_updated_at_triggers("rate_rules")

    op.create_table(
        "rate_changes",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("rule_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=True),
        sa.Column("service_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("grade_id", sa.UUID(), nullable=True),
        sa.Column("old", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("source", sa.Text(), server_default="rates", nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "action IN ('created', 'updated', 'end_dated', 'deleted')",
            name=op.f("ck_rate_changes_action"),
        ),
        sa.CheckConstraint("kind IN ('charge', 'cost')", name=op.f("ck_rate_changes_kind")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_rate_changes_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_changes")),
    )
    op.create_index(
        "ix_rate_changes_organization_id_created_at",
        "rate_changes",
        ["organization_id", "created_at"],
        unique=False,
    )
    for column in ("user_id", "grade_id", "client_id"):
        op.create_index(
            f"ix_rate_changes_organization_id_{column}_created_at",
            "rate_changes",
            ["organization_id", column, "created_at"],
            unique=False,
        )
    op.create_index("ix_rate_changes_rule_id", "rate_changes", ["rule_id"], unique=False)

    op.create_table(
        "rate_reruns",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scope_hash", sa.Text(), nullable=False),
        sa.Column("entries_examined", sa.Integer(), nullable=False),
        sa.Column("entries_changed", sa.Integer(), nullable=False),
        sa.Column("entries_skipped", sa.Integer(), nullable=False),
        sa.Column(
            "skipped",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("charge_value_before", sa.BigInteger(), nullable=False),
        sa.Column("charge_value_after", sa.BigInteger(), nullable=False),
        sa.Column("cost_value_before", sa.BigInteger(), nullable=False),
        sa.Column("cost_value_after", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.UUID(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("source IN ('rerate', 'revalue')", name=op.f("ck_rate_reruns_source")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_rate_reruns_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name=op.f("fk_rate_reruns_requested_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_reruns")),
        sa.UniqueConstraint("organization_id", "id", name="uq_rate_reruns_organization_id_id"),
    )
    op.create_index(
        "ix_rate_reruns_organization_id_created_at",
        "rate_reruns",
        ["organization_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "rate_rerun_items",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("time_entry_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("old_charge_rate", sa.BigInteger(), nullable=False),
        sa.Column("new_charge_rate", sa.BigInteger(), nullable=False),
        sa.Column("old_charge_value", sa.BigInteger(), nullable=False),
        sa.Column("new_charge_value", sa.BigInteger(), nullable=False),
        sa.Column("old_cost_rate", sa.BigInteger(), nullable=False),
        sa.Column("new_cost_rate", sa.BigInteger(), nullable=False),
        sa.Column("old_cost_value", sa.BigInteger(), nullable=False),
        sa.Column("new_cost_value", sa.BigInteger(), nullable=False),
        sa.Column("old_source", sa.Text(), nullable=True),
        sa.Column("new_source", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('changed', 'skipped')", name=op.f("ck_rate_rerun_items_outcome")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["rate_reruns.organization_id", "rate_reruns.id"],
            name="fk_rate_rerun_items_organization_id_run_id_rate_reruns",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_rate_rerun_items_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_rerun_items")),
    )
    op.create_index(
        "ix_rate_rerun_items_organization_id_run_id",
        "rate_rerun_items",
        ["organization_id", "run_id"],
        unique=False,
    )

    op.add_column("time_entries", sa.Column("rate_source", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_time_entries_rate_source"),
        "time_entries",
        f"rate_source IS NULL OR rate_source IN ({SOURCES})",
    )

    # V1 data. A person's staff rate rows form a timeline: each ends the day before the next.
    op.execute(
        """
        INSERT INTO rate_rules (
            id, organization_id, kind, level, user_id, rate, effective_from, effective_to,
            created_by, created_at, updated_at
        )
        SELECT id, organization_id, 'charge', 'staff', user_id, charge_rate, effective_from,
               (lead(effective_from) OVER w) - 1, created_by, created_at, updated_at
        FROM staff_rates
        WINDOW w AS (PARTITION BY organization_id, user_id ORDER BY effective_from)
        """
    )
    op.execute(
        """
        INSERT INTO rate_rules (
            id, organization_id, kind, level, user_id, rate, effective_from, effective_to,
            created_by, created_at, updated_at
        )
        SELECT gen_random_uuid(), organization_id, 'cost', 'staff', user_id, cost_rate,
               effective_from, next_from - 1, created_by, created_at, updated_at
        FROM (
            SELECT *, lead(effective_from) OVER (
                PARTITION BY organization_id, user_id ORDER BY effective_from
            ) AS next_from
            FROM staff_rates
        ) s
        WHERE cost_rate > 0
        """
    )
    op.execute(
        """
        INSERT INTO rate_rules (
            id, organization_id, kind, level, client_id, user_id, rate, effective_from,
            created_at, updated_at
        )
        SELECT id, organization_id, 'charge',
               CASE WHEN user_id IS NULL THEN 'client' ELSE 'client_staff' END,
               client_id, user_id, hourly_rate, DATE '2000-01-01', created_at, updated_at
        FROM client_rates
        """
    )
    op.drop_table("staff_rates")
    op.drop_table("client_rates")


def _create_legacy_tables() -> None:
    op.create_table(
        "staff_rates",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("charge_rate", sa.BigInteger(), nullable=False),
        sa.Column("cost_rate", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("charge_rate >= 0", name=op.f("ck_staff_rates_charge_rate")),
        sa.CheckConstraint("cost_rate >= 0", name=op.f("ck_staff_rates_cost_rate")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_staff_rates_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_staff_rates_organization_id_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_staff_rates_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_rates")),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "effective_from",
            name="uq_staff_rates_organization_id_user_id_effective_from",
        ),
    )
    op.create_table(
        "client_rates",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("hourly_rate", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("hourly_rate >= 0", name=op.f("ck_client_rates_hourly_rate")),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_client_rates_organization_id_client_id_clients",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_client_rates_organization_id_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_client_rates_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_rates")),
    )
    op.create_index(
        "uq_client_rates_client_id_everyone",
        "client_rates",
        ["client_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.create_index(
        "uq_client_rates_client_id_user_id",
        "client_rates",
        ["client_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    add_updated_at_triggers("staff_rates", "client_rates")


def downgrade() -> None:
    _create_legacy_tables()
    # Back to one row per charge rule, with the cost rule in force on its first day. Rules of
    # the other levels (client + service, grade, ...) and the periods have no V1 home.
    op.execute(
        """
        INSERT INTO staff_rates (
            id, organization_id, user_id, effective_from, charge_rate, cost_rate, created_by,
            created_at, updated_at
        )
        SELECT r.id, r.organization_id, r.user_id, r.effective_from, r.rate,
               COALESCE((
                   SELECT c.rate FROM rate_rules c
                   WHERE c.organization_id = r.organization_id AND c.kind = 'cost'
                     AND c.level = 'staff' AND c.user_id = r.user_id
                     AND c.effective_from <= r.effective_from
                     AND (c.effective_to IS NULL OR c.effective_to >= r.effective_from)
                   LIMIT 1
               ), 0),
               r.created_by, r.created_at, r.updated_at
        FROM rate_rules r
        WHERE r.kind = 'charge' AND r.level = 'staff'
        """
    )
    op.execute(
        """
        INSERT INTO client_rates (
            id, organization_id, client_id, user_id, hourly_rate, created_at, updated_at
        )
        SELECT DISTINCT ON (organization_id, client_id, user_id)
               id, organization_id, client_id, user_id, rate, created_at, updated_at
        FROM rate_rules
        WHERE kind = 'charge' AND level IN ('client_staff', 'client')
        ORDER BY organization_id, client_id, user_id, effective_from DESC
        """
    )
    op.drop_constraint(op.f("ck_time_entries_rate_source"), "time_entries", type_="check")
    op.drop_column("time_entries", "rate_source")
    op.drop_index("ix_rate_rerun_items_organization_id_run_id", table_name="rate_rerun_items")
    op.drop_table("rate_rerun_items")
    op.drop_index("ix_rate_reruns_organization_id_created_at", table_name="rate_reruns")
    op.drop_table("rate_reruns")
    op.drop_index("ix_rate_changes_rule_id", table_name="rate_changes")
    for column in ("client_id", "grade_id", "user_id"):
        op.drop_index(
            f"ix_rate_changes_organization_id_{column}_created_at", table_name="rate_changes"
        )
    op.drop_index("ix_rate_changes_organization_id_created_at", table_name="rate_changes")
    op.drop_table("rate_changes")
    for column in ("service_id", "grade_id", "client_id", "user_id"):
        op.drop_index(f"ix_rate_rules_organization_id_{column}", table_name="rate_rules")
    op.drop_table("rate_rules")

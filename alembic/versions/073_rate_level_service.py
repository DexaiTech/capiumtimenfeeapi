"""rate level: service (everyone)

Revision ID: 073
Revises: 070

Ticket APT68: adds the ``service`` rate level, one charge-out rate for a service whoever does the
work (``rate_rules.service_id`` set, no client, no staff member and no grade). It is the last
source of step 5 of the charge-out ladder, after ``service_staff`` and ``service_grade``.
Charge-out only: cost rates stay per person or grade. ``time_entries.rate_source`` may now
record it too.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "073"
down_revision: str | None = "070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_LEVELS = (
    "'client_service_staff', 'client_service', 'client_staff', 'client', "
    "'service_staff', 'service_grade', 'staff', 'grade'"
)
NEW_LEVELS = (
    "'client_service_staff', 'client_service', 'client_staff', 'client', "
    "'service_staff', 'service_grade', 'service', 'staff', 'grade'"
)
OLD_SOURCES = (
    "'client_service_staff', 'client_service', 'client_staff', 'client', 'job', "
    "'service_staff', 'service_grade', 'staff', 'grade', 'default'"
)
NEW_SOURCES = (
    "'client_service_staff', 'client_service', 'client_staff', 'client', 'job', "
    "'service_staff', 'service_grade', 'service', 'staff', 'grade', 'default'"
)


def _scope_check(with_service: bool) -> str:
    def clause(level: str, client: bool, service: bool, user: bool, grade: bool) -> str:
        columns = {"client_id": client, "service_id": service, "user_id": user, "grade_id": grade}
        parts = [f"level = '{level}'"] + [
            f"{name} IS {'NOT NULL' if filled else 'NULL'}" for name, filled in columns.items()
        ]
        return "(" + " AND ".join(parts) + ")"

    clauses = [
        clause("client_service_staff", True, True, True, False),
        clause("client_service", True, True, False, False),
        clause("client_staff", True, False, True, False),
        clause("client", True, False, False, False),
        clause("service_staff", False, True, True, False),
        clause("service_grade", False, True, False, True),
    ]
    if with_service:
        clauses.append(clause("service", False, True, False, False))
    clauses += [
        clause("staff", False, False, True, False),
        clause("grade", False, False, False, True),
    ]
    return " OR ".join(clauses)


def _swap(levels: str, sources: str, scope: str) -> None:
    op.drop_constraint(op.f("ck_rate_rules_level"), "rate_rules", type_="check")
    op.drop_constraint(op.f("ck_rate_rules_scope"), "rate_rules", type_="check")
    op.drop_constraint(op.f("ck_time_entries_rate_source"), "time_entries", type_="check")
    op.create_check_constraint(op.f("ck_rate_rules_level"), "rate_rules", f"level IN ({levels})")
    op.create_check_constraint(op.f("ck_rate_rules_scope"), "rate_rules", scope)
    op.create_check_constraint(
        op.f("ck_time_entries_rate_source"),
        "time_entries",
        f"rate_source IS NULL OR rate_source IN ({sources})",
    )


def upgrade() -> None:
    _swap(NEW_LEVELS, NEW_SOURCES, _scope_check(True))


def downgrade() -> None:
    # Rules of the new level and the entries that were priced by one cannot be kept.
    op.execute("DELETE FROM rate_rules WHERE level = 'service'")
    op.execute("UPDATE time_entries SET rate_source = NULL WHERE rate_source = 'service'")
    _swap(OLD_LEVELS, OLD_SOURCES, _scope_check(False))

"""rate level: staff + service

Revision ID: 057
Revises: 056

Adds the ``service_staff`` rate level: a person's own rate for one service, for every client
(``rate_rules.service_id`` and ``user_id`` set, no client and no grade). It shares step 5 of the
charge-out ladder with ``service_grade`` and wins over it. ``time_entries.rate_source`` may now
record it too.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "057"
down_revision: str | None = "056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_LEVELS = (
    "'client_service_staff', 'client_service', 'client_staff', 'client', "
    "'service_grade', 'staff', 'grade'"
)
NEW_LEVELS = (
    "'client_service_staff', 'client_service', 'client_staff', 'client', "
    "'service_staff', 'service_grade', 'staff', 'grade'"
)
OLD_SOURCES = (
    "'client_service_staff', 'client_service', 'client_staff', 'client', 'job', "
    "'service_grade', 'staff', 'grade', 'default'"
)
NEW_SOURCES = (
    "'client_service_staff', 'client_service', 'client_staff', 'client', 'job', "
    "'service_staff', 'service_grade', 'staff', 'grade', 'default'"
)


def _scope_check(with_service_staff: bool) -> str:
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
    ]
    if with_service_staff:
        clauses.append(clause("service_staff", False, True, True, False))
    clauses += [
        clause("service_grade", False, True, False, True),
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
    op.execute("DELETE FROM rate_rules WHERE level = 'service_staff'")
    op.execute("UPDATE time_entries SET rate_source = NULL WHERE rate_source = 'service_staff'")
    _swap(OLD_LEVELS, OLD_SOURCES, _scope_check(False))

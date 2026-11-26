"""The schema's triggers (MySQL), defined once: ``Base.metadata`` creates them with their tables
(``create_all``) and the migrations call the same functions.

* **No overlapping periods** (PostgreSQL's exclusion constraints): before a row is inserted or
  updated, another row with the same scope whose ``[effective_from, effective_to]`` (inclusive,
  open-ended when ``effective_to`` is NULL) shares a day refuses it with ``SIGNAL SQLSTATE
  '45000'`` and the constraint's name as the message (``app/db/errors.py`` reads it as
  ``overlap``). Writers already hold the scope's named lock (``app/db/locks.py``), so the
  trigger's read cannot race with another writer.
* **Append-only history**: updates and deletes are refused. A practice's rows still go with the
  practice, because MySQL does not fire triggers for foreign-key cascades, and ``TRUNCATE``
  (tests, ``reset_data``) does not fire them either.
"""

from dataclasses import dataclass

from sqlalchemy import DDL, Table, event


@dataclass(frozen=True)
class NoOverlap:
    name: str
    table: str
    # Compared null-safe (``<=>``): two NULLs are the same scope, as coalesce(x, nil) was.
    scope: tuple[str, ...]


NO_OVERLAP = (
    NoOverlap(
        "ex_rate_rules_scope_period",
        "rate_rules",
        ("organization_id", "kind", "client_id", "service_id", "user_id", "grade_id"),
    ),
    NoOverlap(
        "ex_staff_hours_bases_user_period",
        "staff_hours_bases",
        ("organization_id", "user_id"),
    ),
    NoOverlap(
        "ex_overtime_rates_scope_period",
        "overtime_rates",
        ("organization_id", "user_id", "grade_id"),
    ),
)

APPEND_ONLY = (
    "audit_logs",
    "period_lock_events",
    "time_entry_history",
    "wip_movements",
    "wip_snapshots",
    "wip_snapshot_items",
)

_OPEN_END = "'9999-12-31'"


def _no_overlap_body(rule: NoOverlap, *, update: bool) -> str:
    same_scope = " AND ".join(f"o.`{c}` <=> NEW.`{c}`" for c in rule.scope)
    other_row = " AND o.id <> NEW.id" if update else ""
    return (
        # Built from the constants above only, never from input.
        f"IF EXISTS (SELECT 1 FROM `{rule.table}` o WHERE {same_scope}{other_row} "  # noqa: S608
        f"AND o.effective_from <= COALESCE(NEW.effective_to, {_OPEN_END}) "
        f"AND NEW.effective_from <= COALESCE(o.effective_to, {_OPEN_END})) THEN "
        f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{rule.name}'; END IF;"
    )


def create_statements(table: str) -> list[str]:
    """``CREATE TRIGGER`` statements for one table (none for most tables)."""
    statements = []
    for rule in NO_OVERLAP:
        if rule.table != table:
            continue
        for event_name, update in (("INSERT", False), ("UPDATE", True)):
            statements.append(
                f"CREATE TRIGGER `{rule.name}_{event_name.lower()}` BEFORE {event_name} "
                f"ON `{table}` FOR EACH ROW BEGIN {_no_overlap_body(rule, update=update)} END"
            )
    if table in APPEND_ONLY:
        for event_name in ("UPDATE", "DELETE"):
            statements.append(
                f"CREATE TRIGGER `trg_{table}_append_only_{event_name.lower()}` "
                f"BEFORE {event_name} ON `{table}` FOR EACH ROW "
                f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{table} is append-only'"
            )
    return statements


def drop_statements(table: str) -> list[str]:
    names = []
    for rule in NO_OVERLAP:
        if rule.table == table:
            names += [f"{rule.name}_insert", f"{rule.name}_update"]
    if table in APPEND_ONLY:
        names += [f"trg_{table}_append_only_update", f"trg_{table}_append_only_delete"]
    return [f"DROP TRIGGER IF EXISTS `{name}`" for name in names]


def attach(tables: dict[str, Table]) -> None:
    """Create each table's triggers right after the table (``metadata.create_all``)."""
    for name in {rule.table for rule in NO_OVERLAP} | set(APPEND_ONLY):
        for statement in create_statements(name):
            event.listen(tables[name], "after_create", DDL(statement))

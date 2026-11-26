"""What a MySQL error means, from the error SQLAlchemy wraps.

The rest of the application asks ``database_error(exc)`` instead of looking at driver classes:
the kind of failure and, where MySQL names one, the constraint involved.

Overlapping periods (PostgreSQL's exclusion constraints) are enforced by ``BEFORE INSERT`` /
``BEFORE UPDATE`` triggers that ``SIGNAL SQLSTATE '45000'`` with the constraint's name as the
message (``ex_...``), so they arrive here as ``overlap`` with that name.
"""

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.engine import ExceptionContext
from sqlalchemy.exc import IntegrityError

Kind = Literal[
    "unique",
    "foreign_key",
    "check",
    "overlap",
    "rejected",
    "too_long",
    "deadlock",
    "lock_timeout",
    "other",
]

_KINDS: dict[int, Kind] = {
    1062: "unique",  # ER_DUP_ENTRY
    1216: "foreign_key",  # ER_NO_REFERENCED_ROW
    1217: "foreign_key",  # ER_ROW_IS_REFERENCED
    1451: "foreign_key",  # ER_ROW_IS_REFERENCED_2
    1452: "foreign_key",  # ER_NO_REFERENCED_ROW_2
    3819: "check",  # ER_CHECK_CONSTRAINT_VIOLATED
    1406: "too_long",  # ER_DATA_TOO_LONG
    1213: "deadlock",  # ER_LOCK_DEADLOCK
    1205: "lock_timeout",  # ER_LOCK_WAIT_TIMEOUT
    3572: "lock_timeout",  # ER_LOCK_NOWAIT
}
_SIGNAL = 1644  # ER_SIGNAL_EXCEPTION: a trigger's SIGNAL

# "Duplicate entry 'x' for key 'users.uq_users_email_lower'" (MySQL 8 prefixes the table).
_DUPLICATE_KEY = re.compile(r"for key '(?:[^'.]+\.)?([^']+)'")
# "Check constraint 'ck_jobs_status' is violated."
_CHECK = re.compile(r"Check constraint '([^']+)'")
# "... a foreign key constraint fails (`db`.`t`, CONSTRAINT `fk_x` FOREIGN KEY ...)"
_FOREIGN_KEY = re.compile(r"CONSTRAINT `([^`]+)`")


@dataclass(frozen=True)
class DatabaseError:
    number: int
    kind: Kind
    constraint: str | None


def _driver_error(exc: BaseException) -> tuple[int, str] | None:
    """(error number, message) of the MySQL error behind ``exc``, if there is one."""
    seen: set[int] = set()
    current: BaseException | None = getattr(exc, "orig", None) or exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        args: tuple[object, ...] = tuple(getattr(current, "args", ()))
        if len(args) >= 2 and isinstance(args[0], int):
            return args[0], str(args[1])
        current = current.__cause__ or getattr(current, "orig", None)
    return None


def database_error(exc: BaseException) -> DatabaseError | None:
    found = _driver_error(exc)
    if found is None:
        return None
    number, message = found
    if number == _SIGNAL:
        name = message.strip()
        # ``ex_...``: an overlap trigger; anything else: a trigger that refuses the change
        # outright (append-only history, app/db/triggers.py).
        return DatabaseError(number, "overlap" if name.startswith("ex_") else "rejected", name)
    kind = _KINDS.get(number, "other")
    pattern = {"unique": _DUPLICATE_KEY, "check": _CHECK, "foreign_key": _FOREIGN_KEY}.get(kind)
    match = pattern.search(message) if pattern else None
    return DatabaseError(number, kind, match.group(1) if match else None)


# Refusals PostgreSQL raised as integrity errors but PyMySQL raises as OperationalError (a CHECK
# is error 3819, a trigger's SIGNAL 1644).
_INTEGRITY_KINDS = ("check", "overlap", "rejected")


def as_integrity_error(context: ExceptionContext) -> BaseException | None:
    """``handle_error`` rule: a CHECK violation or a trigger's refusal becomes ``IntegrityError``
    again, as on PostgreSQL, so ``except IntegrityError`` in the services still sees it. The
    driver's error stays in ``.orig`` for ``database_error``."""
    if isinstance(context.sqlalchemy_exception, IntegrityError):
        return None
    error = database_error(context.original_exception)
    if error is None or error.kind not in _INTEGRITY_KINDS:
        return None
    return IntegrityError(context.statement, context.parameters, context.original_exception)


def is_overlap(exc: BaseException, constraint: str | None = None) -> bool:
    """True when ``exc`` is an overlap trigger's refusal (optionally, of that constraint)."""
    error = database_error(exc)
    return (
        error is not None
        and error.kind == "overlap"
        and (constraint is None or error.constraint == constraint)
    )

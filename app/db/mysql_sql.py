"""SQL constructs MySQL spells differently, compiled once for the whole application.

* ``func.count(...).filter(cond)`` / ``func.sum(x).filter(cond)``: MySQL has no aggregate
  ``FILTER (WHERE ...)``; the equivalent ``agg(CASE WHEN cond THEN x END)`` is rendered instead
  (``COUNT(*) FILTER`` becomes ``COUNT(CASE WHEN cond THEN 1 END)``). Aggregates ignore NULLs, so
  the result is the same, including NULL for a ``SUM`` / ``MIN`` / ``MAX`` with no matching row.

Imported by ``app/db/session.py`` so the rules are registered before any statement compiles.
"""

from typing import Any

from sqlalchemy import case, func, literal
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.elements import FunctionFilter


@compiles(FunctionFilter, "mysql")
def _aggregate_filter(element: FunctionFilter[Any], compiler: Any, **kw: Any) -> str:
    aggregate = element.func
    condition = element.criterion
    arguments = list(aggregate.clauses)
    if condition is None:
        return str(compiler.process(aggregate, **kw))
    if len(arguments) > 1:
        raise NotImplementedError(f"FILTER on {aggregate.name}() with several arguments")
    # COUNT(*) has ``*`` as its argument: count the matching rows instead.
    value = arguments[0] if arguments and str(arguments[0]) != "*" else literal(1)
    rewritten = getattr(func, aggregate.name)(case((condition, value)), type_=aggregate.type)
    return str(compiler.process(rewritten, **kw))

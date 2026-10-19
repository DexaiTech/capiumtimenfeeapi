"""Delete all application data and keep the schema and the permission catalogue.

Use it to start over on a fresh deployment after test sign-ups. It removes every user,
practice, client, job, time entry, invoice, payment, session and rate-limit
counter. ``alembic_version`` and ``permissions`` (seeded by the migrations) are kept, so
no migration has to run again.

It is a dry run unless ``--yes`` is given, and everything happens in one transaction. Uploaded
files (``STORAGE_DIR``) are not touched: empty that directory yourself to start over.


    python -m app.maintenance.reset_data          # shows what would be deleted
    python -m app.maintenance.reset_data --yes    # deletes it
"""

import argparse
import asyncio
import sys
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings

KEPT_TABLES = frozenset({"alembic_version", "permissions"})

_LIST_TABLES = text(
    """
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
    ORDER BY c.relname
    """
)


async def table_counts(session: AsyncSession) -> dict[str, int]:
    """Row counts of every table that would be emptied (partitions count under their parent)."""
    names = [name for name in (await session.scalars(_LIST_TABLES)) if name not in KEPT_TABLES]
    counts: dict[str, int] = {}
    for name in names:
        # Names come from the catalogue, never from input; they are quoted all the same.
        query = text(f'SELECT count(*) FROM "{name}"')  # noqa: S608 (catalogue name, quoted)
        counts[name] = int(await session.scalar(query) or 0)
    return counts


async def reset_data(session: AsyncSession) -> dict[str, int]:
    """Empty every table except the kept ones; returns the counts that were deleted."""
    counts = await table_counts(session)
    if counts:
        quoted = ", ".join(f'"{name}"' for name in counts)
        await session.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
    return counts


def _describe_target(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port or 5432}{parts.path}"


async def _main(confirmed: bool) -> int:
    url = get_settings().db_url.get_secret_value()
    print(f"Database: {_describe_target(url)}")
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine) as session, session.begin():
            counts = await table_counts(session)
            filled = {name: n for name, n in counts.items() if n}
            if not filled:
                print("Nothing to delete: every table is already empty.")
                return 0
            for name, n in filled.items():
                print(f"  {name}: {n} rows")
            if not confirmed:
                print("\nDry run. Add --yes to delete these rows.")
                return 0
            await reset_data(session)
        print(
            f"\nDeleted the rows of {len(counts)} tables. Kept: {', '.join(sorted(KEPT_TABLES))}."
        )
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--yes", action="store_true", help="really delete (default: dry run)")
    args = parser.parse_args(argv)
    return asyncio.run(_main(args.yes))


if __name__ == "__main__":
    sys.exit(main())

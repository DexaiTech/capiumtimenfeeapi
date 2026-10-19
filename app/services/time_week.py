"""The target figures of the weekly grid (docs/modules/time.md, "Targets and missing days")."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.time_refs import TimeReferenceRepository
from app.schemas.time import WeekGrid, WeekTargets
from app.services.policy import OrgContext
from app.services.time_rules import practice_today
from app.services.time_targets import pattern_of, week_figures


async def with_targets(session: AsyncSession, ctx: OrgContext, grid: WeekGrid) -> WeekGrid:
    """The grid plus the person's targets, working days, missing days and day variance."""
    org = ctx.organization
    member = await TimeReferenceRepository(session).get_member(org.id, grid.user_id)
    today = practice_today(org)
    figures = week_figures(
        pattern_of(org, member),
        grid.days,
        dict(zip(grid.days, grid.day_totals, strict=True)),
        today,
    )
    return grid.model_copy(
        update={
            "today": today,
            "targets": WeekTargets(
                daily_minutes=figures.daily_targets, weekly_minutes=figures.weekly_target
            ),
            "working_days": figures.working_days,
            "missing_days": figures.missing_days,
            "day_variance": figures.day_variance,
        }
    )

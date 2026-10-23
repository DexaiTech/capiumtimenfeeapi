"""Every rate rule as CSV (``GET /rates/export``), in the columns the rates import reads."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.rate_rules import RateRuleRepository, RuleFilters, RuleView
from app.schemas.rate_rules import LEVEL_LABELS, PRECEDENCE, rule_status
from app.services.import_csv import csv_bytes, pounds
from app.services.policy import OrgContext
from app.utils.practice_dates import today_in

HEADER = (
    "kind",
    "staff_code",
    "grade",
    "client_code",
    "service_code",
    "rate",
    "effective_from",
    "effective_to",
    "level",
    "status",
    "staff_name",
    "client_name",
    "service_name",
)


def _order(view: RuleView) -> tuple[object, ...]:
    rule = view.rule
    return (
        rule.kind,
        PRECEDENCE[rule.level],
        view.client_code or "",
        view.service_code or "",
        view.staff_code or "",
        view.grade_code or "",
        rule.effective_from,
    )


class RateExportService:
    def __init__(self, session: AsyncSession) -> None:
        self.rules = RateRuleRepository(session)

    async def export_csv(self, ctx: OrgContext) -> bytes:
        """Every rule of every level, with its status today. The route needs ``rate.read``, the
        permission that shows cost rates everywhere else, so cost rules are never masked."""
        views = await self.rules.list_all(ctx.organization_id, RuleFilters())
        today = today_in(ctx.organization.timezone)
        rows = []
        for view in sorted(views, key=_order):
            rule = view.rule
            rows.append(
                [
                    rule.kind,
                    view.staff_code,
                    view.grade_code,
                    view.client_code,
                    view.service_code,
                    pounds(rule.rate),
                    rule.effective_from,
                    rule.effective_to,
                    LEVEL_LABELS[rule.level],
                    rule_status(rule.effective_from, rule.effective_to, today),
                    view.user_name,
                    view.client_name,
                    view.service_name,
                ]
            )
        return csv_bytes(HEADER, rows)

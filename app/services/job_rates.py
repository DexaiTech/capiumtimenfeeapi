"""The hourly rate of each of a client's jobs and where it comes from (APT54).

A job of a client is billed, by default, at the **task rate**: the rates set against its service
(service + grade, staff + service; not service + grade while grade rates are switched off in the
WIP valuation, APT84), else each person's charge-out rate. The accountant can agree a
custom **client rate** for the job instead: the job's own hourly rate (``jobs.hourly_rate``, level
4 of the charge-out ladder in ``charge_rates``), set or cleared with ``PATCH /jobs/{id}``. A rate
agreed with the client for everyone (the client's rate for the job's service, else for any
service: levels 2 and 3) still comes first, as the ladder has it; the answer names it so the
screen can say so. Fixed-fee jobs bill their fee and non-billable jobs nothing.

The groups are tried in the practice's WIP valuation order and a group switched off never
prices, exactly as in ``charge_rates`` (``client``: the client's rates and the job's own rate,
``service``: the task rate, ``staff``/``grade``: each person's charge-out rate).

Read only; no figure is written. Rule rates need ``rate.read`` (null without it), like ``/rates``;
the job's own rate and fee are part of the job (``job.read``).
"""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.job_rates import ChargeRuleRow, JobRateRepository, JobTermsRow
from app.schemas.job_rates import (
    ClientJobRates,
    ClientRuleRef,
    JobRateRead,
    JobRateSource,
    TaskRate,
)
from app.services.billing_common import get_client_or_404, today_for
from app.services.charge_rates import service_sources
from app.services.policy import OrgContext, has_permission

RATE_PERMISSION = "rate.read"


def _client_rule(job: JobTermsRow, rules: list[ChargeRuleRow]) -> ChargeRuleRow | None:
    """The client's rule for everyone that prices the job before its own rate (levels 2, 3)."""
    if job.service_id is not None:
        for rule in rules:
            if rule.level == "client_service" and rule.service_id == job.service_id:
                return rule
    return next((rule for rule in rules if rule.level == "client"), None)


def describe(
    job: JobTermsRow, rules: list[ChargeRuleRow], show_rates: bool, sources: Sequence[str]
) -> JobRateRead:
    client_on = "client" in sources
    client_rule = _client_rule(job, rules) if client_on else None
    # The service's rates that apply under the WIP valuation, as in ``charge_rates``.
    levels = service_sources(sources) if "service" in sources else ()
    task = [
        rule.rate
        for rule in rules
        if rule.level in levels and job.service_id is not None and rule.service_id == job.service_id
    ]
    person_rules = sum(
        1
        for rule in rules
        if client_on
        and (
            rule.level == "client_staff"
            or (rule.level == "client_service_staff" and rule.service_id == job.service_id)
        )
    )
    source: JobRateSource
    if job.billing_basis == "fixed":
        source = "fixed_fee"
    elif job.billing_basis == "non_billable":
        source = "non_billable"
    else:
        source = "staff"
        for group in sources:
            if group == "client" and client_rule is not None:
                source = "client_rule"
            elif group == "client" and job.hourly_rate is not None:
                source = "job"
            elif group == "service" and task:
                source = "task"
            elif group not in ("staff", "grade"):
                continue
            break
    return JobRateRead(
        job_id=job.id,
        billing_basis=job.billing_basis,
        fixed_fee=job.fixed_fee,
        job_rate=job.hourly_rate,
        task_rate=TaskRate(low=min(task), high=max(task), rules=len(task))
        if task and show_rates
        else None,
        client_rule=ClientRuleRef(
            id=client_rule.id,
            level=client_rule.level,
            rate=client_rule.rate,
            effective_from=client_rule.effective_from,
            effective_to=client_rule.effective_to,
        )
        if client_rule is not None and show_rates
        else None,
        person_rules=person_rules if show_rates else None,
        source=source,
    )


class JobRateService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = JobRateRepository(session)

    async def client_job_rates(self, ctx: OrgContext, client_id: uuid.UUID) -> ClientJobRates:
        org = ctx.organization
        client = await get_client_or_404(
            BillingReferenceRepository(self.session), org.id, client_id
        )
        today = today_for(org)
        jobs = await self.repo.client_jobs(org.id, client.id)
        services = {job.service_id for job in jobs if job.service_id is not None}
        rules = list(await self.repo.charge_rules_on(org.id, today, client.id, services))
        show_rates = has_permission(ctx, RATE_PERMISSION)
        return ClientJobRates(
            client_id=client.id,
            as_of=today,
            show_rates=show_rates,
            jobs=[describe(job, rules, show_rates, org.wip_valuation_sources) for job in jobs],
        )

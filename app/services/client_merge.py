"""Merging one client into another: what happens in this practice when two central clients
become one (docs/modules/central-clients.md, "Merge").

Every row that belongs to the merged-away client (the *source*) moves to the survivor (the
*target*) in one transaction, with the client rows locked: jobs, time entries, timers,
disbursements, WIP write-offs, invoices, credit notes, payments and their allocations, contacts,
services and rate rules. The source is archived and points at the survivor
(``merged_into_client_id``); it is never deleted, so the history that names it stays valid.

Where both clients have the same thing, the target's wins and the source's copy stays on the
archived source (and is listed in the audit entry):

* a service the target takes already: the target's agreement (fee, frequency, dates) stands;
* a rate rule whose period overlaps a rule of the same kind and scope on the target (two such
  rules cannot coexist): the target's rate stands;
* a primary contact when the target has one: it moves as an ordinary contact.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, RateChange, RateRule
from app.repositories.client_merge import (
    MOVED_TABLES,
    ClientMergeRepository,
    MergeCounts,
)

RATE_HISTORY_SOURCE = "client_merge"


def _rate_snapshot(rule: RateRule, client_id: uuid.UUID) -> dict[str, Any]:
    return {
        "client_id": str(client_id),
        "rate": rule.rate,
        "effective_from": rule.effective_from.isoformat(),
        "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
    }


async def merge_clients(
    session: AsyncSession, organization_id: uuid.UUID, source: Client, target: Client
) -> MergeCounts:
    """Move everything of ``source`` to ``target`` and archive ``source``. Run inside the
    caller's transaction; the caller audits and logs it."""
    repo = ClientMergeRepository(session)
    await repo.lock_pair(organization_id, (source.id, target.id))
    counts = MergeCounts()
    counts.moved = await repo.repoint(organization_id, source.id, target.id)

    contacts, demoted = await repo.move_contacts(organization_id, source.id, target.id)
    counts.moved["client_contacts"] = contacts
    counts.contacts_demoted = demoted

    engagements, left_services = await repo.move_engagements(organization_id, source.id, target.id)
    counts.moved["client_services"] = engagements
    if left_services:
        counts.left_behind["client_services"] = left_services

    rules, left_rules = await repo.move_rate_rules(organization_id, source.id, target.id)
    counts.moved["rate_rules"] = len(rules)
    if left_rules:
        counts.left_behind["rate_rules"] = left_rules
    # The rate history is append-only: each moved rule gets a row saying where it came from.
    await repo.add_rate_history(
        [
            RateChange(
                organization_id=organization_id,
                rule_id=rule.id,
                action="updated",
                kind=rule.kind,
                level=rule.level,
                client_id=target.id,
                service_id=rule.service_id,
                user_id=rule.user_id,
                grade_id=rule.grade_id,
                old=_rate_snapshot(rule, source.id),
                new=_rate_snapshot(rule, target.id),
                actor_user_id=None,
                source=RATE_HISTORY_SOURCE,
            )
            for rule in rules
        ]
    )
    counts.rate_history_rows = len(rules)

    counts.merged_clients_repointed = await repo.repoint_merged_sources(
        organization_id, source.id, target.id
    )
    source.status = "archived"
    source.merged_into_client_id = target.id
    await session.flush()
    # Every table of MOVED_TABLES is reported, zero or not.
    for table in MOVED_TABLES:
        counts.moved.setdefault(table, 0)
    return counts

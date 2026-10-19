"""Helpers for the onboarding checklist tests."""

from typing import Any

from httpx import AsyncClient, Response

from tests.factories import Actor
from tests.time_helpers import org_url

__all__ = ["STEP_KEYS", "checklist", "mark", "steps"]

# The order of the steps is part of the contract (docs/modules/onboarding.md).
STEP_KEYS = [
    "practice_details",
    "vat_registration",
    "staff_added",
    "grades_rates",
    "services_activity_types",
    "vat_codes_reviewed",
    "numbering_configured",
    "invoice_template",
    "clients_added",
    "jobs_created",
    "first_time_entry",
    "period_lock",
    "integrations",
]


async def checklist(client: AsyncClient, actor: Actor, org: dict) -> Response:
    return await client.get(org_url(org, "/onboarding"), headers=actor.headers)


async def mark(
    client: AsyncClient, actor: Actor, org: dict, key: str, state: str, **extra: Any
) -> Response:
    return await client.put(
        org_url(org, f"/onboarding/steps/{key}"),
        headers=actor.headers,
        json={"state": state, **extra},
    )


def steps(response: Response) -> dict[str, dict[str, Any]]:
    """The steps of a checklist response by key."""
    assert response.status_code == 200, response.text
    return {step["key"]: step for step in response.json()["data"]["steps"]}

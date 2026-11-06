"""Shared set-up of the overtime capture, approval and Staff Cost report tests (APT81)."""

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from httpx import AsyncClient

from tests.clients_helpers import sql
from tests.domain_factories import make_client, make_job, make_staff_rate, practice, staff
from tests.factories import Actor
from tests.team_helpers import grade_id, set_grade
from tests.time_helpers import log_time, ok_data, org_url, set_practice, time_job

# A Monday well in the past (weekly periods run Monday to Sunday).
MONDAY = date(2026, 9, 7)


@dataclass
class Scene:
    owner: Actor
    org: dict
    person: Actor
    acme: Any
    job_x: Any
    internal_job: uuid.UUID


async def add_hours(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    person: Actor,
    *,
    basis: str = "weekly",
    standard_minutes: int = 2250,
    effective_from: str = "2026-01-01",
) -> dict:
    return ok_data(
        await client.post(
            org_url(org, f"/staff/{person.id}/hours"),
            headers=actor.headers,
            json={
                "basis": basis,
                "standard_minutes": standard_minutes,
                "effective_from": effective_from,
            },
        ),
        201,
    )


async def add_overtime_rate(client: AsyncClient, actor: Actor, org: dict, **body: Any) -> dict:
    payload = {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in body.items()}
    payload.setdefault("effective_from", "2026-01-01")
    return ok_data(
        await client.post(org_url(org, "/overtime-rates"), headers=actor.headers, json=payload),
        201,
    )


async def scene(
    client: AsyncClient,
    *,
    role: str = "staff",
    basis: str = "weekly",
    standard_minutes: int = 2250,
    enabled: bool = True,
    grade_bp: int | None = 15_000,
) -> Scene:
    """A practice with overtime on; Sam Senior (grade SNR) on £60 charge / £20 cost an hour,
    with working hours from 1 January 2026 and the grade's overtime rate (1.5x); Job X of Acme
    and the Internal client's administration job."""
    owner, org = await practice(client)
    await set_practice(org, time_increment_minutes=1, overtime_enabled=enabled)
    person = await staff(org, role, full_name="Sam Senior")
    await set_grade(org, person, "SNR")
    await make_staff_rate(org, person, 6_000, 2_000)
    await add_hours(client, owner, org, person, basis=basis, standard_minutes=standard_minutes)
    if grade_bp is not None:
        await add_overtime_rate(
            client,
            owner,
            org,
            level="grade",
            grade_id=await grade_id(org, "SNR"),
            mode="multiplier",
            multiplier_bp=grade_bp,
        )
    acme = await make_client(org, "Acme Ltd")
    job_x = await make_job(org, acme, "Job X")
    internal = await time_job(org)
    assert internal is not None
    return Scene(owner, org, person, acme, job_x, internal)


async def log(
    client: AsyncClient,
    s: Scene,
    day: date,
    minutes: int,
    job_id: Any = None,
    *,
    confirm: bool = False,
    status: int = 201,
    actor: Actor | None = None,
) -> Any:
    """``POST /time-entries`` for Sam (on Job X unless another job is given)."""
    job = job_id if job_id is not None else s.job_x.id
    body: dict[str, Any] = {"job_id": job, "entry_date": day, "minutes": minutes}
    if confirm:
        body["confirm_overtime"] = True
    return await log_time(client, actor or s.person, s.org, status=status, **body)


async def overtime_rows(org: dict) -> dict[str, dict]:
    """``time_entry_id -> row`` of the practice's overtime rows."""
    rows = await sql("SELECT * FROM time_entry_overtime WHERE organization_id = :o", o=org["id"])
    return {str(r["time_entry_id"]): r for r in rows}


async def is_overtime(entry_id: str) -> bool:
    rows = await sql("SELECT is_overtime FROM time_entries WHERE id = :i", i=entry_id)
    return bool(rows[0]["is_overtime"])

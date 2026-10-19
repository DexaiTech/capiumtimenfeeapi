"""Shared helpers for the jobs, time, timer and timesheet tests."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import select, text, update

from app.db.models import (
    Invoice,
    InvoiceLine,
    Organization,
    OrganizationMember,
    RateRule,
    TimeEntry,
    Timer,
    Timesheet,
    WipAllocation,
    WipWriteOff,
)
from app.db.session import get_sessionmaker
from tests.factories import API, Actor

# A Monday well in the past, so "this week" never interferes.
MONDAY = date(2026, 9, 7)
RATE_EPOCH = date(2000, 1, 1)


def org_url(org: dict, path: str) -> str:
    return f"{API}/organizations/{org['id']}{path}"


def ok_data(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json()["data"]


def err(response: Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    return response.json()["error"]


async def set_practice(org: dict, **values: Any) -> None:
    """Change practice settings directly (time_increment_minutes, week_start, ...)."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Organization).where(Organization.id == uuid.UUID(org["id"])).values(**values)
        )
        await session.commit()


async def set_manager(org: dict, member: Actor, manager: Actor | None) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(OrganizationMember)
            .where(
                OrganizationMember.organization_id == uuid.UUID(org["id"]),
                OrganizationMember.user_id == member.id,
            )
            .values(manager_user_id=manager.id if manager else None)
        )
        await session.commit()


async def make_client_rate(
    org: dict, client_id: uuid.UUID, hourly_rate: int, user: Actor | None = None
) -> None:
    """A client charge rate: for one person (client + staff) or everyone (client)."""
    async with get_sessionmaker()() as session:
        session.add(
            RateRule(
                organization_id=uuid.UUID(org["id"]),
                kind="charge",
                level="client_staff" if user else "client",
                client_id=client_id,
                user_id=user.id if user else None,
                rate=hourly_rate,
                effective_from=RATE_EPOCH,
            )
        )
        await session.commit()


async def entry_row(entry_id: str | uuid.UUID) -> TimeEntry:
    async with get_sessionmaker()() as session:
        row = await session.get(TimeEntry, uuid.UUID(str(entry_id)))
        assert row is not None
        return row


async def set_entry(entry_id: str | uuid.UUID, **values: Any) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(TimeEntry).where(TimeEntry.id == uuid.UUID(str(entry_id))).values(**values)
        )
        await session.commit()


async def set_sheet(sheet_id: str | uuid.UUID, **values: Any) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Timesheet).where(Timesheet.id == uuid.UUID(str(sheet_id))).values(**values)
        )
        await session.commit()


async def sheet_count(org: dict) -> int:
    async with get_sessionmaker()() as session:
        found = await session.scalar(
            text("SELECT count(*) FROM timesheets WHERE organization_id = :org"),
            {"org": org["id"]},
        )
    return int(found or 0)


async def age_timer(org: dict, user: Actor, seconds: int) -> None:
    """Pretend the person's running timer started ``seconds`` ago."""
    async with get_sessionmaker()() as session:
        timer = await session.scalar(
            select(Timer).where(
                Timer.organization_id == uuid.UUID(org["id"]), Timer.user_id == user.id
            )
        )
        assert timer is not None
        if timer.started_at is not None:
            timer.started_at = datetime.now(UTC) - timedelta(seconds=seconds)
        else:
            timer.accumulated_seconds = seconds
        await session.commit()


async def log_time(
    client: AsyncClient, actor: Actor, org: dict, *, status: int = 201, **body: Any
) -> Any:
    body.setdefault("entry_date", MONDAY.isoformat())
    body.setdefault("minutes", 60)
    payload = {k: (str(v) if isinstance(v, uuid.UUID | date) else v) for k, v in body.items()}
    response = await client.post(org_url(org, "/time-entries"), headers=actor.headers, json=payload)
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else response.json()["error"]


async def bill_items(
    org: dict,
    client_id: uuid.UUID,
    items: list[tuple[str, uuid.UUID, int, int]],
    *,
    invoice_status: str = "issued",
    reversed_: bool = False,
) -> uuid.UUID:
    """Put WIP items on an invoice: ``(kind 'time'|'disbursement', id, wip, billed)``."""
    org_id = uuid.UUID(org["id"])
    net = sum(billed for *_, billed in items)
    async with get_sessionmaker()() as session:
        invoice = Invoice(
            organization_id=org_id,
            client_id=client_id,
            status=invoice_status,
            number=None if invoice_status == "draft" else f"INV-T{uuid.uuid4().hex[:8]}",
            issue_date=None if invoice_status == "draft" else MONDAY,
            due_date=None if invoice_status == "draft" else MONDAY + timedelta(days=30),
            currency="GBP",
            net_total=net,
            vat_total=0,
            gross_total=net,
        )
        session.add(invoice)
        await session.flush()
        line = InvoiceLine(
            organization_id=org_id,
            invoice_id=invoice.id,
            position=1,
            kind="manual",
            description="Work",
            unit_amount=net,
            net_amount=net,
            vat_rate_bp=0,
            vat_amount=0,
        )
        session.add(line)
        await session.flush()
        for kind, item_id, wip, billed in items:
            session.add(
                WipAllocation(
                    organization_id=org_id,
                    time_entry_id=item_id if kind == "time" else None,
                    disbursement_id=item_id if kind == "disbursement" else None,
                    invoice_id=invoice.id,
                    invoice_line_id=line.id,
                    wip_value=wip,
                    billed_value=billed,
                    reversed_at=datetime.now(UTC) if reversed_ else None,
                )
            )
        await session.commit()
        return invoice.id


async def write_off_items(
    org: dict, client_id: uuid.UUID, items: list[tuple[str, uuid.UUID, int]]
) -> uuid.UUID:
    org_id = uuid.UUID(org["id"])
    async with get_sessionmaker()() as session:
        write_off = WipWriteOff(
            organization_id=org_id,
            client_id=client_id,
            written_off_on=MONDAY,
            reason="Not recoverable",
            total_wip_value=sum(w for *_, w in items),
        )
        session.add(write_off)
        await session.flush()
        for kind, item_id, wip in items:
            session.add(
                WipAllocation(
                    organization_id=org_id,
                    time_entry_id=item_id if kind == "time" else None,
                    disbursement_id=item_id if kind == "disbursement" else None,
                    write_off_id=write_off.id,
                    wip_value=wip,
                    billed_value=0,
                )
            )
        await session.commit()
        return write_off.id

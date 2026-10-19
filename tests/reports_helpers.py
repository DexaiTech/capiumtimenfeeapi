"""Direct-insert helpers for the report and dashboard tests.

Billing rows (invoices, lines, WIP allocations, write-offs, credit notes, payments) are
written straight into the tables with consistent totals: ``gross = net + vat`` per
invoice, ``paid_total``/``credited_total`` equal to the allocations and credit notes, and
items moved to the billing status their allocation implies.
"""

import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select, update

from app.db.models import (
    Client,
    CreditNote,
    CreditNoteLine,
    Disbursement,
    Invoice,
    InvoiceLine,
    Job,
    JobAssignee,
    Organization,
    OrganizationMember,
    Payment,
    PaymentAllocation,
    Permission,
    Role,
    RolePermission,
    TimeEntry,
    Timer,
    WipAllocation,
    WipWriteOff,
)
from app.db.session import get_sessionmaker
from app.services import reports as report_service
from app.utils.money import vat_on
from tests.factories import API, Actor

_counter = itertools.count(1)
Item = TimeEntry | Disbursement


def _uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def report_url(org: dict, name: str) -> str:
    return f"{API}/organizations/{org['id']}/reports/{name}"


def dashboard_url(org: dict) -> str:
    return f"{API}/organizations/{org['id']}/dashboard"


async def fetch(client: AsyncClient, actor: Actor, org: dict, name: str, **params: Any) -> Response:
    clean = {k: str(v) for k, v in params.items() if v is not None}
    return await client.get(report_url(org, name), headers=actor.headers, params=clean)


async def report(client: AsyncClient, actor: Actor, org: dict, name: str, **params: Any) -> dict:
    """The report's ``data`` ({rows, totals, filters}); asserts 200."""
    response = await fetch(client, actor, org, name, **params)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def pin_now(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    """Make the report and dashboard services believe it is ``instant`` (UTC)."""
    monkeypatch.setattr(report_service, "utc_now", lambda: instant)


async def set_org(org: dict, **fields: Any) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Organization).where(Organization.id == _uuid(org["id"])).values(**fields)
        )
        await session.commit()


async def set_member(org: dict, user: Actor, **fields: Any) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(OrganizationMember)
            .where(
                OrganizationMember.organization_id == _uuid(org["id"]),
                OrganizationMember.user_id == user.id,
            )
            .values(**fields)
        )
        await session.commit()


async def update_row(model: Any, row_id: uuid.UUID, **fields: Any) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(update(model).where(model.id == row_id).values(**fields))
        await session.commit()


async def custom_role(org: dict, name: str, codes: set[str]) -> None:
    """A custom role holding exactly ``codes``; give it with ``add_member(org_id, name)``."""
    async with get_sessionmaker()() as session:
        role = Role(organization_id=_uuid(org["id"]), name=name)
        session.add(role)
        await session.flush()
        ids = await session.scalars(select(Permission.id).where(Permission.code.in_(codes)))
        for permission_id in ids:
            session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        await session.commit()


async def assign(org: dict, job: Job, user: Actor) -> None:
    async with get_sessionmaker()() as session:
        session.add(JobAssignee(organization_id=_uuid(org["id"]), job_id=job.id, user_id=user.id))
        await session.commit()


async def make_timer(
    org: dict,
    user: Actor,
    *,
    started_at: datetime | None,
    accumulated_seconds: int = 0,
    client: Client | None = None,
    job: Job | None = None,
    description: str | None = None,
) -> Timer:
    row = Timer(
        organization_id=_uuid(org["id"]),
        user_id=user.id,
        client_id=client.id if client else None,
        job_id=job.id if job else None,
        started_at=started_at,
        accumulated_seconds=accumulated_seconds,
        description=description,
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


@dataclass
class LineSpec:
    net: int
    vat_bp: int = 2000
    kind: str = "manual"
    job: Job | None = None
    vat_rate_id: uuid.UUID | None = None
    description: str = "Professional services"


@dataclass
class MadeInvoice:
    invoice: Invoice
    lines: list[InvoiceLine]

    @property
    def id(self) -> uuid.UUID:
        return self.invoice.id

    @property
    def gross(self) -> int:
        return self.invoice.gross_total


async def make_invoice(
    org: dict,
    client: Client,
    lines: list[LineSpec] | None = None,
    *,
    issue_date: date | None = date(2026, 9, 15),
    due_date: date | None = None,
    status: str = "issued",
) -> MadeInvoice:
    """An invoice with lines; totals are the sums of the lines (VAT rounded per line)."""
    specs = lines or [LineSpec(1_000)]
    nets = [s.net for s in specs]
    vats = [vat_on(s.net, s.vat_bp) for s in specs]
    number = next(_counter)
    org_id = _uuid(org["id"])
    invoice = Invoice(
        organization_id=org_id,
        client_id=client.id,
        number=None if status == "draft" else f"INV-T{number:05d}",
        status=status,
        issue_date=issue_date,
        due_date=due_date or (issue_date + timedelta(days=30) if issue_date else None),
        currency="GBP",
        net_total=sum(nets),
        vat_total=sum(vats),
        gross_total=sum(nets) + sum(vats),
        issued_at=datetime.now(UTC) if status != "draft" else None,
        voided_at=datetime.now(UTC) if status == "void" else None,
        void_reason="Raised in error" if status == "void" else None,
    )
    async with get_sessionmaker()() as session:
        session.add(invoice)
        await session.flush()
        rows = []
        for position, (spec, vat) in enumerate(zip(specs, vats, strict=True), start=1):
            line = InvoiceLine(
                organization_id=org_id,
                invoice_id=invoice.id,
                position=position,
                kind=spec.kind,
                description=spec.description,
                job_id=spec.job.id if spec.job else None,
                unit_amount=spec.net,
                net_amount=spec.net,
                vat_rate_id=spec.vat_rate_id,
                vat_rate_bp=spec.vat_bp,
                vat_amount=vat,
            )
            session.add(line)
            rows.append(line)
        await session.commit()
        await session.refresh(invoice)
        for line in rows:
            await session.refresh(line)
    return MadeInvoice(invoice, rows)


def _item_kwargs(item: Item) -> dict[str, Any]:
    if isinstance(item, TimeEntry):
        return {"time_entry_id": item.id}
    return {"disbursement_id": item.id}


def _wip_value(item: Item) -> int:
    return item.charge_value if isinstance(item, TimeEntry) else item.amount


async def _set_status(session: Any, item: Item, status: str) -> None:
    model = type(item)
    await session.execute(update(model).where(model.id == item.id).values(billing_status=status))


async def allocate(
    org: dict,
    made: MadeInvoice,
    item: Item,
    billed_value: int | None = None,
    *,
    line: int = 0,
    reversed_at: datetime | None = None,
) -> WipAllocation:
    """Put ``item`` on line ``line`` of the invoice at ``billed_value`` (default its WIP
    value). The item becomes ``billed`` (``in_draft`` on a draft, ``unbilled`` when the
    allocation is reversed)."""
    row = WipAllocation(
        organization_id=_uuid(org["id"]),
        invoice_id=made.id,
        invoice_line_id=made.lines[line].id,
        wip_value=_wip_value(item),
        billed_value=_wip_value(item) if billed_value is None else billed_value,
        reversed_at=reversed_at,
        **_item_kwargs(item),
    )
    if reversed_at is not None:
        status = "unbilled"
    else:
        status = "in_draft" if made.invoice.status == "draft" else "billed"
    async with get_sessionmaker()() as session:
        session.add(row)
        await _set_status(session, item, status)
        await session.commit()
        await session.refresh(row)
    return row


async def write_off(
    org: dict,
    client: Client,
    items: list[Item],
    *,
    written_off_on: date = date(2026, 9, 25),
    job: Job | None = None,
) -> WipWriteOff:
    org_id = _uuid(org["id"])
    row = WipWriteOff(
        organization_id=org_id,
        client_id=client.id,
        job_id=job.id if job else None,
        written_off_on=written_off_on,
        reason="Unrecoverable",
        total_wip_value=sum(_wip_value(i) for i in items),
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.flush()
        for item in items:
            session.add(
                WipAllocation(
                    organization_id=org_id,
                    write_off_id=row.id,
                    wip_value=_wip_value(item),
                    billed_value=0,
                    **_item_kwargs(item),
                )
            )
            await _set_status(session, item, "written_off")
        await session.commit()
        await session.refresh(row)
    return row


async def credit_note(
    org: dict,
    made: MadeInvoice,
    lines: list[tuple[int, int]],
    *,
    issue_date: date = date(2026, 9, 25),
    vat_rate_id: uuid.UUID | None = None,
) -> CreditNote:
    """A credit note against the invoice with ``(net, vat_bp)`` lines; raises the
    invoice's ``credited_total``."""
    org_id = _uuid(org["id"])
    vats = [vat_on(net, bp) for net, bp in lines]
    net = sum(n for n, _ in lines)
    number = next(_counter)
    row = CreditNote(
        organization_id=org_id,
        client_id=made.invoice.client_id,
        invoice_id=made.id,
        number=f"CN-T{number:05d}",
        issue_date=issue_date,
        reason="Fee reduction",
        net_total=net,
        vat_total=sum(vats),
        gross_total=net + sum(vats),
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.flush()
        for position, ((line_net, bp), vat) in enumerate(zip(lines, vats, strict=True), 1):
            session.add(
                CreditNoteLine(
                    organization_id=org_id,
                    credit_note_id=row.id,
                    position=position,
                    description="Credit",
                    net_amount=line_net,
                    vat_rate_id=vat_rate_id,
                    vat_rate_bp=bp,
                    vat_amount=vat,
                )
            )
        await session.execute(
            update(Invoice)
            .where(Invoice.id == made.id)
            .values(credited_total=Invoice.credited_total + row.gross_total)
        )
        await session.commit()
        await session.refresh(row)
    return row


async def payment(
    org: dict,
    client: Client,
    amount: int,
    *,
    received_on: date = date(2026, 9, 30),
    allocations: list[tuple[MadeInvoice, int]] | None = None,
    voided: bool = False,
) -> Payment:
    """A payment, allocated as given (raising each invoice's ``paid_total``)."""
    org_id = _uuid(org["id"])
    allocations = allocations or []
    allocated = sum(a for _, a in allocations)
    row = Payment(
        organization_id=org_id,
        client_id=client.id,
        received_on=received_on,
        amount=amount,
        method="bank_transfer",
        allocated_total=allocated,
        voided_at=datetime.now(UTC) if voided else None,
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.flush()
        for made, share in allocations:
            session.add(
                PaymentAllocation(
                    organization_id=org_id,
                    client_id=client.id,
                    payment_id=row.id,
                    invoice_id=made.id,
                    amount=share,
                )
            )
            await session.execute(
                update(Invoice)
                .where(Invoice.id == made.id)
                .values(paid_total=Invoice.paid_total + share)
            )
        await session.commit()
        await session.refresh(row)
    return row

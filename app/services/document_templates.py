"""Which invoice template a document is drawn with, and the item details its layouts list.

A document uses the template of the client (``clients.invoice_template_id``, even when it was
deactivated since), else the practice's default; a preview may name another one. The
``detailed`` layout lists the time entries, disbursements and expense lines each invoice line
bills (from its WIP allocations), the ``summary`` layout needs the names of the jobs.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, Disbursement, Expense, InvoiceLine, TimeEntry
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.invoice_templates import InvoiceTemplateRepository
from app.repositories.wip import WipRepository
from app.services.invoice_pdf import ItemDetail, PdfTemplate


async def pdf_template(
    session: AsyncSession,
    organization_id: uuid.UUID,
    client: Client,
    template_id: uuid.UUID | None = None,
) -> PdfTemplate:
    """The template of ``template_id`` (a preview), else the client's, else the practice
    default; the plain standard look when the practice somehow has none."""
    templates = InvoiceTemplateRepository(session)
    row = await templates.get(organization_id, template_id) if template_id else None
    if row is None and client.invoice_template_id is not None:
        row = await templates.get(organization_id, client.invoice_template_id)
    if row is None:
        row = await templates.get_default(organization_id)
    if row is None:
        return PdfTemplate()
    return PdfTemplate(
        layout=row.layout,
        show_logo=row.show_logo,
        show_bank_details=row.show_bank_details,
        footer_text=row.footer_text,
    )


async def job_names(
    session: AsyncSession, organization_id: uuid.UUID, lines: Sequence[InvoiceLine]
) -> dict[uuid.UUID, str]:
    jobs = await BillingReferenceRepository(session).jobs_by_ids(
        organization_id, [line.job_id for line in lines]
    )
    return {job_id: job.name for job_id, job in jobs.items()}


async def line_details(
    session: AsyncSession, organization_id: uuid.UUID, invoice_id: uuid.UUID
) -> dict[uuid.UUID, list[ItemDetail]]:
    """The items each invoice line bills, by line id, oldest first: date, who, narrative,
    minutes (time only) and the share of the line's amount the item carries."""
    allocations = await WipRepository(session).allocations_for_invoice(organization_id, invoice_id)
    allocations = [a for a in allocations if a.invoice_line_id is not None]
    if not allocations:
        return {}
    entries = {
        e.id: e
        for e in await _rows(
            session, TimeEntry, organization_id, [a.time_entry_id for a in allocations]
        )
    }
    disbursements = {
        d.id: d
        for d in await _rows(
            session, Disbursement, organization_id, [a.disbursement_id for a in allocations]
        )
    }
    expenses = {
        x.id: x
        for x in await _rows(session, Expense, organization_id, [a.expense_id for a in allocations])
    }
    names = await BillingReferenceRepository(session).member_names(
        organization_id, [e.user_id for e in entries.values()]
    )
    found: dict[uuid.UUID, list[ItemDetail]] = defaultdict(list)
    for allocation in allocations:
        assert allocation.invoice_line_id is not None
        if allocation.time_entry_id and (entry := entries.get(allocation.time_entry_id)):
            item = ItemDetail(
                day=entry.entry_date,
                who=names.get(entry.user_id),
                narrative=entry.description or "Time",
                minutes=entry.minutes,
                amount=allocation.billed_value,
            )
        elif allocation.disbursement_id and (
            disbursement := disbursements.get(allocation.disbursement_id)
        ):
            item = ItemDetail(
                day=disbursement.incurred_on,
                who=disbursement.supplier,
                narrative=disbursement.description,
                minutes=None,
                amount=allocation.billed_value,
            )
        elif allocation.expense_id and (expense := expenses.get(allocation.expense_id)):
            item = ItemDetail(
                day=expense.incurred_on,
                who=expense.supplier,
                narrative=expense.description,
                minutes=None,
                amount=allocation.billed_value,
            )
        else:
            continue
        found[allocation.invoice_line_id].append(item)
    return {line_id: sorted(items, key=lambda i: i.day) for line_id, items in found.items()}


async def _rows[T: TimeEntry | Disbursement | Expense](
    session: AsyncSession,
    model: type[T],
    organization_id: uuid.UUID,
    ids: Sequence[uuid.UUID | None],
) -> list[T]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return []
    stmt = select(model).where(model.organization_id == organization_id, model.id.in_(wanted))
    return list((await session.execute(stmt)).scalars())

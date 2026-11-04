"""What a new practice starts with: UK VAT rates, activity codes, common services, staff
grades, ticket tags, expense types, number sequences, the Standard invoice template, an empty
period lock and the built-in Internal client with a non-billable job per non-chargeable activity
code. Everything here but the Internal client can be changed afterwards in Settings."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ActivityCode,
    Client,
    DocumentSequence,
    ExpenseType,
    Grade,
    InvoiceTemplate,
    Job,
    Organization,
    PeriodLock,
    Service,
    TicketTag,
    VatRate,
)
from app.services.sequences import DEFAULT_SEQUENCES
from app.utils.ids import new_id

# The built-in client internal time goes on (APT64); migration 078 gives it to older practices.
INTERNAL_CLIENT_CODE = "INTERNAL"
INTERNAL_CLIENT_NAME = "Internal"


def internal_job_number(activity_code: str) -> str:
    """The number of the Internal client's job for a non-chargeable activity code."""
    return f"INT-{activity_code}"


# (name, code, rate in basis points, is default)
UK_VAT_RATES: tuple[tuple[str, str, int, bool], ...] = (
    ("Standard 20%", "standard", 2000, True),
    ("Reduced 5%", "reduced", 500, False),
    ("Zero rated", "zero", 0, False),
    ("Exempt", "exempt", 0, False),
    ("Outside the scope", "outside_scope", 0, False),
)

# (code, name, chargeable)
ACTIVITY_CODES: tuple[tuple[str, str, bool], ...] = (
    ("ACC", "Accounts preparation", True),
    ("BKP", "Bookkeeping", True),
    ("VAT", "VAT returns", True),
    ("PAY", "Payroll", True),
    ("TAX", "Tax returns", True),
    ("ADV", "Advisory and meetings", True),
    ("SEC", "Company secretarial", True),
    ("ADM", "Practice administration", False),
    ("TRN", "Training and CPD", False),
    ("BDV", "Business development", False),
    ("HOL", "Holiday and absence", False),
)

# (code, name, billing basis, default fee in pence or None)
SERVICES: tuple[tuple[str, str, str, int | None], ...] = (
    ("ACCS", "Year-end accounts", "fixed", 120_000),
    ("CT", "Corporation tax return", "fixed", 45_000),
    ("SA", "Self assessment tax return", "fixed", 25_000),
    ("VAT", "VAT return", "fixed", 15_000),
    ("PAYROLL", "Payroll", "fixed", 10_000),
    ("BOOKS", "Bookkeeping", "hourly", None),
    ("ADVICE", "Advisory", "hourly", None),
    ("SECRETARIAL", "Company secretarial", "fixed", 7_500),
)

# (code, name, chargeable target in basis points). No default rates: the practice sets them.
GRADES: tuple[tuple[str, str, int], ...] = (
    ("PTR", "Partner", 5000),
    ("MGR", "Manager", 7000),
    ("SNR", "Senior", 7500),
    ("JNR", "Junior", 8000),
)

# (name, colour): the labels of the Application Testing tickets.
TICKET_TAGS: tuple[tuple[str, str], ...] = (
    ("Bug", "red"),
    ("Enhancement", "violet"),
    ("UI", "blue"),
    ("Reporting", "green"),
    ("Accounts", "blue"),
    ("Validation", "red"),
    ("Trial Balance", "violet"),
)

# The invoice template every practice starts with, as its default.
DEFAULT_INVOICE_TEMPLATE = "Standard"

# (name, code, kind of the default VAT rate, is mileage, billable by default)
EXPENSE_TYPES: tuple[tuple[str, str, str, bool, bool], ...] = (
    ("Travel", "TRAVEL", "zero", False, True),
    ("Mileage", "MILEAGE", "outside_scope", True, True),
    ("Subsistence", "SUBSISTENCE", "standard", False, False),
    ("Accommodation", "ACCOM", "standard", False, True),
    ("Postage", "POSTAGE", "exempt", False, True),
    ("Printing", "PRINTING", "standard", False, True),
    ("Software", "SOFTWARE", "standard", False, False),
    ("Other", "OTHER", "standard", False, False),
)


async def seed_practice_defaults(session: AsyncSession, organization: Organization) -> None:
    standard_id = None
    rate_ids: dict[str, uuid.UUID] = {}
    for name, code, rate_bp, is_default in UK_VAT_RATES:
        rate = VatRate(
            id=new_id(),
            organization_id=organization.id,
            name=name,
            code=code,
            rate_bp=rate_bp,
            is_default=is_default,
        )
        session.add(rate)
        rate_ids[code] = rate.id
        if is_default:
            standard_id = rate.id
    # Services reference the rates through a composite key with no ORM relationship, so
    # the flush order is not implied: write the rates first.
    await session.flush()
    for code, name, chargeable in ACTIVITY_CODES:
        session.add(
            ActivityCode(
                organization_id=organization.id, code=code, name=name, is_chargeable=chargeable
            )
        )
    internal = Client(
        id=new_id(),
        organization_id=organization.id,
        code=INTERNAL_CLIENT_CODE,
        name=INTERNAL_CLIENT_NAME,
        client_type="other",
        is_internal=True,
    )
    session.add(internal)
    # Jobs reference the client through a composite key: write the client first.
    await session.flush()
    for code, name, chargeable in ACTIVITY_CODES:
        if not chargeable:
            session.add(
                Job(
                    organization_id=organization.id,
                    client_id=internal.id,
                    number=internal_job_number(code),
                    name=name,
                    status="in_progress",
                    billing_basis="non_billable",
                )
            )
    for order, (code, name, basis, fee) in enumerate(SERVICES):
        session.add(
            Service(
                organization_id=organization.id,
                code=code,
                name=name,
                billing_basis=basis,
                default_fee=fee,
                vat_rate_id=standard_id,
                sort_order=order,
            )
        )
    for order, (code, name, target_bp) in enumerate(GRADES):
        session.add(
            Grade(
                organization_id=organization.id,
                code=code,
                name=name,
                default_chargeable_target_bp=target_bp,
                sort_order=order,
            )
        )
    for name, code, vat_kind, is_mileage, billable in EXPENSE_TYPES:
        session.add(
            ExpenseType(
                organization_id=organization.id,
                name=name,
                code=code,
                default_vat_rate_id=rate_ids[vat_kind],
                is_mileage=is_mileage,
                default_billable=billable,
            )
        )
    for kind, (prefix, padding) in DEFAULT_SEQUENCES.items():
        session.add(
            DocumentSequence(
                organization_id=organization.id, kind=kind, prefix=prefix, padding=padding
            )
        )
    for name, color in TICKET_TAGS:
        session.add(TicketTag(organization_id=organization.id, name=name, color=color))
    session.add(
        InvoiceTemplate(
            organization_id=organization.id,
            name=DEFAULT_INVOICE_TEMPLATE,
            layout="standard",
            show_logo=True,
            show_bank_details=True,
            is_default=True,
            status="active",
        )
    )
    # The row writes take a share lock on to learn whether a period is locked (nothing is).
    session.add(PeriodLock(organization_id=organization.id))
    await session.flush()

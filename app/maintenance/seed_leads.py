"""Add sample leads to a practice, for demos and testing on local and staging databases.

Never for production: the command refuses when ``APP_ENV=production``.

    python -m app.maintenance.seed_leads --practice-slug acme            # shows what it would add
    python -m app.maintenance.seed_leads --practice-slug acme --yes      # adds them
    python -m app.maintenance.seed_leads --practice-slug acme --count 6 --yes

The leads are a fixed list of fictional UK prospects (emails on ``example.com`` /
``example.co.uk``, Ofcom drama phone numbers), so every run adds the same ones. Most stay open;
two are converted into clients and two archived. The command acts as the practice's oldest
active Owner through ``LeadService``, so validation and the audit trail are those of the app.
Every sample's notes start with ``Sample lead (seed)``. A sample whose name the practice already
has is skipped, so running it again adds nothing. While the practice uses branches, a conversion
takes the first active branch; with none, the leads to convert stay open.

It is a dry run unless ``--yes`` is given; everything is added in one transaction. It prints the
target database as host:port/database, never the credentials.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.exceptions import AppException
from app.db.models import Lead, Organization
from app.db.session import unit_of_work
from app.maintenance.bootstrap import BootstrapError, _ensure_schema_is_current
from app.maintenance.reset_data import _describe_target
from app.repositories.branches import BranchRepository
from app.schemas.clients import ClientType
from app.schemas.leads import LeadConvert, LeadCreate
from app.services.engagement_jobs import system_context
from app.services.leads import LeadService

MARKER = "Sample lead (seed)"
Outcome = Literal["open", "converted", "archived"]


class SeedError(Exception):
    """Something the operator can fix; the message is safe to print."""


@dataclass(frozen=True, slots=True)
class SampleLead:
    name: str
    client_type: ClientType
    contact_name: str
    email: str
    phone: str
    source: str
    acquisition_cost: int  # pence
    onboarding_cost: int  # pence
    note: str
    outcome: Outcome = "open"

    def create_data(self) -> LeadCreate:
        return LeadCreate(
            name=self.name,
            client_type=self.client_type,
            contact_name=self.contact_name,
            email=self.email,
            phone=self.phone,
            source=self.source,
            notes=f"{MARKER}. {self.note}",
            acquisition_cost=self.acquisition_cost,
            onboarding_cost=self.onboarding_cost,
        )


SAMPLE_LEADS: tuple[SampleLead, ...] = (
    SampleLead(
        "Hartley & Sons Joinery Ltd", "limited_company", "Tom Hartley",
        "tom@hartleyjoinery.example.co.uk", "0113 496 0412", "Referral", 15_000, 25_000,
        "Year-end accounts and corporation tax; payroll for six staff.",
    ),
    SampleLead(
        "Priya Patel Dental Practice", "sole_trader", "Dr Priya Patel",
        "priya@pateldental.example.com", "0161 496 0733", "Website", 4_500, 12_000,
        "Self assessment and quarterly VAT returns. Wants a fixed monthly fee.",
        "converted",
    ),
    SampleLead(
        "Greenway Landscapes LLP", "llp", "Sarah Greenway",
        "sarah@greenwaylandscapes.example.co.uk", "07700 900218", "Networking event", 7_500,
        18_000, "Met at the chamber of commerce breakfast. Members' returns and bookkeeping.",
    ),
    SampleLead(
        "The Old Mill Tearooms", "partnership", "James and Ellen Ward",
        "hello@oldmilltearooms.example.co.uk", "0117 496 0158", "Existing client", 0, 8_000,
        "Introduced by a current client. Partnership return and Making Tax Digital set-up.",
    ),
    SampleLead(
        "Northbridge IT Consulting Ltd", "limited_company", "Daniel Osei",
        "daniel@northbridgeit.example.com", "020 7946 0381", "Accountant switch", 20_000,
        35_000, "Unhappy with the current accountant. Needs professional clearance first.",
        "archived",
    ),
    SampleLead(
        "Fletcher Family Trust", "trust", "Margaret Fletcher",
        "m.fletcher@fletchertrust.example.co.uk", "0121 496 0627", "Referral", 10_000, 15_000,
        "Trust tax return (SA900) and beneficiary statements.",
    ),
    SampleLead(
        "Coastline Surf School Ltd", "limited_company", "Jake Pengelly",
        "jake@coastlinesurf.example.co.uk", "07700 900544", "Website", 6_000, 20_000,
        "Seasonal business; cloud bookkeeping and payroll for summer staff.",
        "converted",
    ),
    SampleLead(
        "Amelia Clarke Photography", "sole_trader", "Amelia Clarke",
        "amelia@clarkephotography.example.com", "07700 900671", "Networking event", 2_500,
        6_000, "Self assessment only for now; may register for VAT next year.",
    ),
    SampleLead(
        "St Aidan's Community Hall", "charity", "Revd Mark Holloway",
        "treasurer@staidanshall.example.co.uk", "0191 498 0245",
        "Existing client", 0, 5_000, "Independent examination of the charity accounts.",
    ),
    SampleLead(
        "Brookfield Property Lettings Ltd", "limited_company", "Rachel Brook",
        "rachel@brookfieldlettings.example.com", "0113 496 0896", "Accountant switch", 12_500,
        22_500, "Buy-to-let portfolio of 14 properties. Went with a cheaper quote.",
        "archived",
    ),
    SampleLead(
        "Mohammed Iqbal", "individual", "Mohammed Iqbal",
        "m.iqbal@example.co.uk", "07700 900903", "Referral", 1_500, 3_000,
        "Landlord with two rental properties; capital gains on a sale this year.",
    ),
    SampleLead(
        "Kestrel Engineering Ltd", "limited_company", "Graham Kerr",
        "graham@kestreleng.example.co.uk", "0141 496 0574", "Website", 18_000, 30_000,
        "R&D tax credit claim and management accounts each quarter.",
    ),
    SampleLead(
        "Willow & Thyme Florists", "partnership", "Hannah Webb",
        "hannah@willowandthyme.example.com", "0117 496 0921", "Networking event", 3_000,
        9_000, "Two partners; wants help with the move to Making Tax Digital.",
    ),
    SampleLead(
        "Ashworth Haulage Ltd", "limited_company", "Paul Ashworth",
        "paul@ashworthhaulage.example.co.uk", "0161 496 0287", "Accountant switch", 25_000,
        40_000, "Fleet of 12 lorries; payroll, CIS and VAT. Year end 31 March.",
    ),
)  # fmt: skip


@dataclass(slots=True)
class SeedResult:
    practice_name: str
    practice_slug: str
    owner_name: str
    owner_email: str
    applied: bool
    added: list[SampleLead] = field(default_factory=list)
    skipped: list[SampleLead] = field(default_factory=list)
    conversion_skipped: str | None = None

    def status_of(self, sample: SampleLead) -> Outcome:
        """The status an added sample ends with (a lead to convert stays open when it cannot)."""
        if sample.outcome == "converted" and self.conversion_skipped:
            return "open"
        return sample.outcome

    def with_status(self, status: Outcome) -> list[SampleLead]:
        return [s for s in self.added if self.status_of(s) == status]


def refuse_in_production() -> None:
    if get_settings().is_production:
        raise SeedError("Refusing to add sample leads: APP_ENV is production. Nothing was changed.")


async def seed_leads(
    session: AsyncSession, *, practice_slug: str, count: int = 12, apply: bool = False
) -> SeedResult:
    """Add the first ``count`` sample leads the practice does not have yet (only with
    ``apply``; otherwise it only works out what it would add)."""
    refuse_in_production()
    if not 1 <= count <= len(SAMPLE_LEADS):
        raise SeedError(f"--count must be 1 to {len(SAMPLE_LEADS)}.")
    await _ensure_schema_is_current(session)
    organization = await session.scalar(
        select(Organization).where(
            Organization.slug == practice_slug, Organization.deleted_at.is_(None)
        )
    )
    if organization is None:
        raise SeedError(f"No practice has the slug {practice_slug!r}.")
    ctx = await system_context(session, organization.id)
    if ctx is None:
        raise SeedError("The practice has no active Owner to act as. Nothing was changed.")
    taken = set(
        await session.scalars(
            select(func.lower(Lead.name)).where(Lead.organization_id == organization.id)
        )
    )
    result = SeedResult(
        organization.name, organization.slug, ctx.user.full_name, ctx.user.email, applied=apply
    )
    for sample in SAMPLE_LEADS[:count]:
        (result.skipped if sample.name.lower() in taken else result.added).append(sample)

    branch_id = None
    if ctx.organization.branches_enabled:
        branches = await BranchRepository(session).list_all(organization.id, is_active=True)
        if branches:
            branch_id = branches[0].id
        else:
            result.conversion_skipped = (
                "the practice uses branches but has no active branch, so the leads to convert "
                "stay open"
            )

    if not apply:
        await session.commit()  # ends the read-only transaction; nothing was written
        return result

    service = LeadService(session)
    rctx = RequestContext()
    async with unit_of_work(session):
        for sample in result.added:
            lead = await service.create(ctx, sample.create_data(), rctx)
            if sample.outcome == "converted" and not result.conversion_skipped:
                await service.convert(ctx, lead.id, LeadConvert(branch_id=branch_id), rctx)
            elif sample.outcome == "archived":
                await service.set_archived(ctx, lead.id, True, rctx)
    return result


def _report(result: SeedResult) -> None:
    print(f"Practice: {result.practice_name} ({result.practice_slug})")
    print(f"Acting as: {result.owner_name} <{result.owner_email}> (oldest active Owner)")
    for sample in result.skipped:
        print(f"  = {sample.name}: already in the practice, skipped")
    for sample in result.added:
        print(
            f"  + {sample.name} ({sample.client_type}, {sample.source}): {result.status_of(sample)}"
        )
    if result.conversion_skipped:
        print(f"Not converting: {result.conversion_skipped}.")
    counts = ", ".join(
        f"{len(result.with_status(o))} {o}" for o in ("open", "converted", "archived")
    )
    if not result.added:
        print("Nothing to add: the practice already has these sample leads.")
    elif result.applied:
        print(f"\nAdded {len(result.added)} sample leads ({counts}).")
    else:
        print(f"\nWould add {len(result.added)} sample leads ({counts}).")
        print("Dry run. Add --yes to add them.")


async def _main(args: argparse.Namespace) -> int:
    try:
        refuse_in_production()
    except SeedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    url = get_settings().database_url
    print(f"Database: {_describe_target(url)}")
    engine = create_async_engine(url)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            result = await seed_leads(
                session, practice_slug=args.practice_slug, count=args.count, apply=args.yes
            )
    except (SeedError, BootstrapError, AppException) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    _report(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--practice-slug", required=True, help="slug of the practice to fill")
    parser.add_argument(
        "--count",
        type=int,
        default=12,
        help=f"how many of the {len(SAMPLE_LEADS)} sample leads to use (default 12)",
    )
    parser.add_argument("--yes", action="store_true", help="really add them (default: dry run)")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

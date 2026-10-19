"""Practice settings: VAT rates, services, activity codes and number sequences.

Rules (docs/domain.md, "Practice settings"):

* VAT rates are archived, never deleted, once anything references them; at most one live
  default. Making a rate the default unsets the old one in the same transaction, under a
  lock on all the practice's rates (taken in id order).
* Services and activity codes are deactivated, never deleted, once used.
* Numbering changes need ``organization.update``; invoice, credit-note, estimate and ticket
  numbers only move forward (409 ``SEQUENCE_BACKWARDS``).
"""

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models import ActivityCode, DocumentSequence, Service, VatRate
from app.db.session import unit_of_work
from app.repositories.settings import (
    ActivityCodeRepository,
    SequenceRepository,
    ServiceRepository,
    VatRateRepository,
)
from app.schemas.settings import (
    ActivityCodeCreate,
    ActivityCodeRead,
    ActivityCodeUpdate,
    DeleteOutcome,
    SequenceUpdate,
    ServiceActivityCodesRead,
    ServiceActivityCodesUpdate,
    ServiceCreate,
    ServiceRead,
    ServiceUpdate,
    VatRateCreate,
    VatRateUpdate,
)
from app.services.common import apply_changes, duplicate_error, record_audit
from app.services.policy import OrgContext
from app.services.sequences import DEFAULT_SEQUENCES, format_number

FORWARD_ONLY_SEQUENCES = frozenset({"invoice", "credit_note", "ticket", "estimate"})
# Sequences whose numbers become codes: letters, digits, '_' and '-', at most 20 characters.
CODE_SEQUENCES = {"client": "Client", "staff": "Staff"}
CODE_MAX_LENGTH = 20
_CODE_PREFIX = re.compile(r"^[A-Za-z0-9_-]*$")


def _now() -> datetime:
    return datetime.now(UTC)


class SettingsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.vat_rates = VatRateRepository(session)
        self.services = ServiceRepository(session)
        self.activities = ActivityCodeRepository(session)
        self.sequences = SequenceRepository(session)

    # --- VAT rates ----------------------------------------------------------------------

    async def list_vat_rates(self, ctx: OrgContext, *, include_archived: bool) -> list[VatRate]:
        return await self.vat_rates.list(ctx.organization_id, include_archived=include_archived)

    async def get_vat_rate(self, ctx: OrgContext, rate_id: uuid.UUID) -> VatRate:
        rate = await self.vat_rates.get(ctx.organization_id, rate_id)
        if rate is None:
            raise NotFoundError("VAT rate not found.")
        return rate

    async def create_vat_rate(
        self, ctx: OrgContext, data: VatRateCreate, rctx: RequestContext
    ) -> VatRate:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            await self.vat_rates.lock_all(org_id)
            if await self.vat_rates.name_taken(org_id, data.name):
                raise duplicate_error(
                    "name", "A VAT rate with this name exists.", "VAT_RATE_NAME_TAKEN"
                )
            if data.is_default:
                await self.vat_rates.clear_default(org_id, keep_id=None)
            rate = await self.vat_rates.add(
                VatRate(
                    organization_id=org_id,
                    name=data.name,
                    code=data.code,
                    rate_bp=data.rate_bp,
                    is_default=data.is_default,
                )
            )
            await record_audit(
                self.session,
                ctx,
                rctx,
                "vat_rate.created",
                "vat_rate",
                rate.id,
                {"name": rate.name, "rate_bp": rate.rate_bp, "is_default": rate.is_default},
            )
        return rate

    async def update_vat_rate(
        self, ctx: OrgContext, rate_id: uuid.UUID, data: VatRateUpdate, rctx: RequestContext
    ) -> VatRate:
        org_id = ctx.organization_id
        values = data.model_dump(exclude_unset=True)
        async with unit_of_work(self.session):
            await self.vat_rates.lock_all(org_id)
            rate = await self.vat_rates.get(org_id, rate_id, lock=True)
            if rate is None:
                raise NotFoundError("VAT rate not found.")
            archive = values.pop("is_archived", None)
            make_default = values.pop("is_default", None)
            archived = rate.archived_at is not None if archive is None else archive
            if make_default and archived:
                raise ValidationError.for_field(
                    "is_default", "An archived VAT rate cannot be the default."
                )
            name = values.get("name", rate.name)
            if not archived and await self.vat_rates.name_taken(org_id, name, exclude_id=rate.id):
                raise duplicate_error(
                    "name", "A VAT rate with this name exists.", "VAT_RATE_NAME_TAKEN"
                )

            changes = apply_changes(rate, values)
            if archive is True and rate.archived_at is None:
                rate.archived_at = _now()
                changes["archived"] = [False, True]
            elif archive is False and rate.archived_at is not None:
                rate.archived_at = None
                changes["archived"] = [True, False]
            if archived and rate.is_default:
                changes["is_default"] = [True, False]
                rate.is_default = False
            if make_default is True and not rate.is_default:
                await self.vat_rates.clear_default(org_id, keep_id=rate.id)
                rate.is_default = True
                changes["is_default"] = [False, True]
            elif make_default is False and rate.is_default:
                rate.is_default = False
                changes["is_default"] = [True, False]
            rate = await self.vat_rates.save(rate)
            if changes:
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "vat_rate.updated",
                    "vat_rate",
                    rate.id,
                    {"name": rate.name, "changes": changes},
                )
        return rate

    async def delete_vat_rate(
        self, ctx: OrgContext, rate_id: uuid.UUID, rctx: RequestContext
    ) -> DeleteOutcome:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            rate = await self.vat_rates.get(org_id, rate_id, lock=True)
            if rate is None:
                raise NotFoundError("VAT rate not found.")
            meta = {"name": rate.name, "rate_bp": rate.rate_bp}
            if await self.vat_rates.is_used(org_id, rate.id):
                if rate.archived_at is None:
                    rate.archived_at = _now()
                    rate.is_default = False
                    await self.vat_rates.save(rate)
                    await record_audit(
                        self.session, ctx, rctx, "vat_rate.archived", "vat_rate", rate.id, meta
                    )
                return DeleteOutcome(outcome="archived", id=rate.id)
            await self.vat_rates.delete(org_id, rate.id)
            await record_audit(
                self.session, ctx, rctx, "vat_rate.deleted", "vat_rate", rate.id, meta
            )
        return DeleteOutcome(outcome="deleted", id=rate_id)

    async def _check_vat_rate(self, org_id: uuid.UUID, rate_id: uuid.UUID | None) -> None:
        if rate_id is None:
            return
        rate = await self.vat_rates.get(org_id, rate_id)
        if rate is None or rate.archived_at is not None:
            raise ValidationError.for_field(
                "vat_rate_id", "Choose a live VAT rate of the practice."
            )

    # --- Services -----------------------------------------------------------------------

    async def _service_reads(self, ctx: OrgContext, services: list[Service]) -> list[ServiceRead]:
        ids = [s.id for s in services]
        links = await self.services.activity_ids(ctx.organization_id, ids)
        used = await self.services.used_ids(ctx.organization_id, ids)
        return [
            ServiceRead.from_model(s, activity_code_ids=links.get(s.id, []), is_used=s.id in used)
            for s in services
        ]

    async def _service_read(self, ctx: OrgContext, service: Service) -> ServiceRead:
        [read] = await self._service_reads(ctx, [service])
        return read

    async def _get_service(
        self, ctx: OrgContext, service_id: uuid.UUID, *, lock: bool = False
    ) -> Service:
        service = await self.services.get(ctx.organization_id, service_id, lock=lock)
        if service is None:
            raise NotFoundError("Service not found.")
        return service

    async def list_services(
        self, ctx: OrgContext, *, include_inactive: bool, q: str | None
    ) -> list[ServiceRead]:
        services = await self.services.list_services(
            ctx.organization_id, include_inactive=include_inactive, q=q
        )
        return await self._service_reads(ctx, services)

    async def get_service(self, ctx: OrgContext, service_id: uuid.UUID) -> ServiceRead:
        return await self._service_read(ctx, await self._get_service(ctx, service_id))

    async def create_service(
        self, ctx: OrgContext, data: ServiceCreate, rctx: RequestContext
    ) -> ServiceRead:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            await self._ensure_service_unique(org_id, data.code, data.name, exclude_id=None)
            await self._check_vat_rate(org_id, data.vat_rate_id)
            service = await self.services.add(Service(organization_id=org_id, **data.model_dump()))
            await record_audit(
                self.session,
                ctx,
                rctx,
                "service.created",
                "service",
                service.id,
                {"code": service.code, "name": service.name, "default_fee": service.default_fee},
            )
            result = await self._service_read(ctx, service)
        return result

    async def update_service(
        self, ctx: OrgContext, service_id: uuid.UUID, data: ServiceUpdate, rctx: RequestContext
    ) -> ServiceRead:
        org_id = ctx.organization_id
        values = data.model_dump(exclude_unset=True)
        async with unit_of_work(self.session):
            service = await self._get_service(ctx, service_id, lock=True)
            await self._ensure_service_unique(
                org_id, values.get("code"), values.get("name"), exclude_id=service.id
            )
            if values.get("vat_rate_id") not in (None, service.vat_rate_id):
                await self._check_vat_rate(org_id, values["vat_rate_id"])
            changes = apply_changes(service, values)
            service = await self.services.save(service)
            if changes:
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "service.updated",
                    "service",
                    service.id,
                    {"code": service.code, "name": service.name, "changes": changes},
                )
            result = await self._service_read(ctx, service)
        return result

    async def set_service_active(
        self, ctx: OrgContext, service_id: uuid.UUID, active: bool, rctx: RequestContext
    ) -> ServiceRead:
        return await self.update_service(ctx, service_id, ServiceUpdate(is_active=active), rctx)

    async def delete_service(
        self, ctx: OrgContext, service_id: uuid.UUID, rctx: RequestContext
    ) -> DeleteOutcome:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            service = await self._get_service(ctx, service_id, lock=True)
            meta = {"code": service.code, "name": service.name}
            if await self.services.is_used(org_id, service.id):
                if service.is_active:
                    service.is_active = False
                    await self.services.save(service)
                    await record_audit(
                        self.session, ctx, rctx, "service.deactivated", "service", service.id, meta
                    )
                return DeleteOutcome(outcome="deactivated", id=service.id)
            await self.services.delete(org_id, service.id)
            await record_audit(
                self.session, ctx, rctx, "service.deleted", "service", service.id, meta
            )
        return DeleteOutcome(outcome="deleted", id=service_id)

    async def _ensure_service_unique(
        self, org_id: uuid.UUID, code: str | None, name: str | None, *, exclude_id: uuid.UUID | None
    ) -> None:
        if code is not None and await self.services.code_taken(org_id, code, exclude_id=exclude_id):
            raise duplicate_error(
                "code", "A service with this code already exists.", "SERVICE_CODE_TAKEN"
            )
        if name is not None and await self.services.name_taken(org_id, name, exclude_id=exclude_id):
            raise duplicate_error(
                "name", "A service with this name already exists.", "SERVICE_NAME_TAKEN"
            )

    # --- Service to activity links ----------------------------------------------------------

    async def _links_read(self, ctx: OrgContext, service: Service) -> ServiceActivityCodesRead:
        org_id = ctx.organization_id
        ids = (await self.services.activity_ids(org_id, [service.id])).get(service.id, [])
        found = await self.activities.get_many(org_id, ids)
        used = await self.activities.used_ids(org_id, ids)
        activities = sorted(found.values(), key=lambda a: a.code)
        return ServiceActivityCodesRead(
            service_id=service.id,
            restricted=bool(ids),
            activity_codes=[
                ActivityCodeRead.from_model(a, is_used=a.id in used) for a in activities
            ],
        )

    async def get_service_activity_codes(
        self, ctx: OrgContext, service_id: uuid.UUID
    ) -> ServiceActivityCodesRead:
        return await self._links_read(ctx, await self._get_service(ctx, service_id))

    async def set_service_activity_codes(
        self,
        ctx: OrgContext,
        service_id: uuid.UUID,
        data: ServiceActivityCodesUpdate,
        rctx: RequestContext,
    ) -> ServiceActivityCodesRead:
        """Replace the activity codes a service's jobs may record time against."""
        org_id = ctx.organization_id
        wanted = list(dict.fromkeys(data.activity_code_ids))
        async with unit_of_work(self.session):
            service = await self._get_service(ctx, service_id, lock=True)
            before = (await self.services.activity_ids(org_id, [service.id])).get(service.id, [])
            codes = await self.activities.get_many(org_id, {*wanted, *before})
            if any(a not in codes for a in wanted):
                raise ValidationError.for_field(
                    "activity_code_ids", "Some of these activity codes do not exist."
                )
            if set(before) != set(wanted):
                await self.services.replace_activity_ids(org_id, service.id, wanted)
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "service.activity_codes_changed",
                    "service",
                    service.id,
                    {
                        "code": service.code,
                        "name": service.name,
                        "added": sorted(codes[a].code for a in set(wanted) - set(before)),
                        "removed": sorted(codes[a].code for a in set(before) - set(wanted)),
                        "restricted": bool(wanted),
                    },
                )
            result = await self._links_read(ctx, service)
        return result

    # --- Activity codes -----------------------------------------------------------------

    async def _activity_reads(
        self, ctx: OrgContext, activities: list[ActivityCode]
    ) -> list[ActivityCodeRead]:
        used = await self.activities.used_ids(ctx.organization_id, [a.id for a in activities])
        return [ActivityCodeRead.from_model(a, is_used=a.id in used) for a in activities]

    async def _activity_read(self, ctx: OrgContext, activity: ActivityCode) -> ActivityCodeRead:
        [read] = await self._activity_reads(ctx, [activity])
        return read

    async def _get_activity(
        self, ctx: OrgContext, activity_id: uuid.UUID, *, lock: bool = False
    ) -> ActivityCode:
        activity = await self.activities.get(ctx.organization_id, activity_id, lock=lock)
        if activity is None:
            raise NotFoundError("Activity code not found.")
        return activity

    async def list_activity_codes(
        self, ctx: OrgContext, *, include_inactive: bool, q: str | None
    ) -> list[ActivityCodeRead]:
        codes = await self.activities.list_activity_codes(
            ctx.organization_id, include_inactive=include_inactive, q=q
        )
        return await self._activity_reads(ctx, codes)

    async def get_activity_code(self, ctx: OrgContext, activity_id: uuid.UUID) -> ActivityCodeRead:
        return await self._activity_read(ctx, await self._get_activity(ctx, activity_id))

    async def create_activity_code(
        self, ctx: OrgContext, data: ActivityCodeCreate, rctx: RequestContext
    ) -> ActivityCodeRead:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            await self._ensure_activity_unique(org_id, data.code, data.name, exclude_id=None)
            await self._check_vat_rate(org_id, data.vat_rate_id)
            activity = await self.activities.add(
                ActivityCode(organization_id=org_id, **data.model_dump())
            )
            await record_audit(
                self.session,
                ctx,
                rctx,
                "activity_code.created",
                "activity_code",
                activity.id,
                {"code": activity.code, "name": activity.name},
            )
            result = await self._activity_read(ctx, activity)
        return result

    async def update_activity_code(
        self,
        ctx: OrgContext,
        activity_id: uuid.UUID,
        data: ActivityCodeUpdate,
        rctx: RequestContext,
    ) -> ActivityCodeRead:
        org_id = ctx.organization_id
        values = data.model_dump(exclude_unset=True)
        async with unit_of_work(self.session):
            activity = await self._get_activity(ctx, activity_id, lock=True)
            await self._ensure_activity_unique(
                org_id, values.get("code"), values.get("name"), exclude_id=activity.id
            )
            if values.get("vat_rate_id") not in (None, activity.vat_rate_id):
                await self._check_vat_rate(org_id, values["vat_rate_id"])
            changes = apply_changes(activity, values)
            activity = await self.activities.save(activity)
            if changes:
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "activity_code.updated",
                    "activity_code",
                    activity.id,
                    {"code": activity.code, "changes": changes},
                )
            result = await self._activity_read(ctx, activity)
        return result

    async def set_activity_code_active(
        self, ctx: OrgContext, activity_id: uuid.UUID, active: bool, rctx: RequestContext
    ) -> ActivityCodeRead:
        return await self.update_activity_code(
            ctx, activity_id, ActivityCodeUpdate(is_active=active), rctx
        )

    async def delete_activity_code(
        self, ctx: OrgContext, activity_id: uuid.UUID, rctx: RequestContext
    ) -> DeleteOutcome:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            activity = await self._get_activity(ctx, activity_id, lock=True)
            meta = {"code": activity.code, "name": activity.name}
            if await self.activities.is_used(org_id, activity.id):
                if activity.is_active:
                    activity.is_active = False
                    await self.activities.save(activity)
                    await record_audit(
                        self.session,
                        ctx,
                        rctx,
                        "activity_code.deactivated",
                        "activity_code",
                        activity.id,
                        meta,
                    )
                return DeleteOutcome(outcome="deactivated", id=activity.id)
            await self.activities.delete(org_id, activity.id)
            await record_audit(
                self.session,
                ctx,
                rctx,
                "activity_code.deleted",
                "activity_code",
                activity.id,
                meta,
            )
        return DeleteOutcome(outcome="deleted", id=activity_id)

    async def _ensure_activity_unique(
        self, org_id: uuid.UUID, code: str | None, name: str | None, *, exclude_id: uuid.UUID | None
    ) -> None:
        if code is not None and await self.activities.code_taken(
            org_id, code, exclude_id=exclude_id
        ):
            raise duplicate_error(
                "code",
                "An activity code with this code already exists.",
                "ACTIVITY_CODE_TAKEN",
            )
        if name is not None and await self.activities.name_taken(
            org_id, name, exclude_id=exclude_id
        ):
            raise duplicate_error(
                "name",
                "An activity code with this name already exists.",
                "ACTIVITY_NAME_TAKEN",
            )

    # --- Number sequences ---------------------------------------------------------------

    async def list_sequences(self, ctx: OrgContext) -> list[DocumentSequence]:
        """Every kind, in a fixed order; a kind never used yet shows its defaults."""
        found = {s.kind: s for s in await self.sequences.list(ctx.organization_id)}
        result = []
        for kind, (prefix, padding) in DEFAULT_SEQUENCES.items():
            result.append(
                found.get(kind)
                or DocumentSequence(
                    organization_id=ctx.organization_id,
                    kind=kind,
                    prefix=prefix,
                    padding=padding,
                    next_number=1,
                )
            )
        return result

    async def update_sequence(
        self, ctx: OrgContext, kind: str, data: SequenceUpdate, rctx: RequestContext
    ) -> DocumentSequence:
        org_id = ctx.organization_id
        values = data.model_dump(exclude_unset=True)
        async with unit_of_work(self.session):
            sequence = await self.sequences.lock(org_id, kind)
            if sequence is None:
                prefix, padding = DEFAULT_SEQUENCES[kind]
                sequence = await self.sequences.add(
                    DocumentSequence(
                        organization_id=org_id,
                        kind=kind,
                        prefix=prefix,
                        padding=padding,
                        next_number=1,
                    )
                )
                sequence = await self.sequences.lock(org_id, kind) or sequence
            new_next = values.get("next_number", sequence.next_number)
            if kind in FORWARD_ONLY_SEQUENCES and new_next < sequence.next_number:
                raise ConflictError(
                    "Invoice, credit note, estimate and ticket numbers can only move forward.",
                    code="SEQUENCE_BACKWARDS",
                    details={"current_next_number": sequence.next_number},
                )
            prefix = values.get("prefix", sequence.prefix)
            padding = values.get("padding", sequence.padding)
            preview = format_number(prefix, new_next, padding)
            if kind in CODE_SEQUENCES:
                label = CODE_SEQUENCES[kind]
                if not _CODE_PREFIX.match(prefix):
                    raise ValidationError.for_field(
                        "prefix", f"{label} codes allow only letters, digits, '_' and '-'."
                    )
                if len(preview) > CODE_MAX_LENGTH:
                    raise ValidationError.for_field(
                        "padding", f"{label} codes are at most 20 characters long."
                    )
            elif await self.sequences.number_used(org_id, kind, preview):
                raise ConflictError(
                    f"The number {preview} is already used.",
                    code="SEQUENCE_NUMBER_TAKEN",
                    details={"preview": preview},
                )
            changes = apply_changes(sequence, values)
            sequence = await self.sequences.save(sequence)
            if changes:
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "sequence.updated",
                    "sequence",
                    None,
                    {"kind": kind, "changes": changes},
                )
        return sequence

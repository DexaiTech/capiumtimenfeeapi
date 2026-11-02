"""Invoice templates: how a practice's invoices look (layout, logo, bank details, footer).

Rules (docs/domain.md, "Invoice templates"):

* Every practice has one default template, ``Standard`` to begin with. The default cannot be
  deleted or deactivated (409 ``TEMPLATE_IS_DEFAULT``): make another template the default first.
* A template a client uses cannot be deleted (409 ``TEMPLATE_IN_USE``): deactivate it instead.
  Deleting is a soft delete, so its name becomes free again.
* Names are unique among live templates, ignoring case (409 ``TEMPLATE_NAME_TAKEN``).
* Anyone in the practice reads the active templates; changing and the inactive ones need
  ``settings.manage``.
* The watermark is a text or a picture, never both (422): the picture is checked and stored like
  a profile picture (PNG, JPEG, WebP or GIF, up to 2 MB), in the practice's file store.
"""

import uuid
from collections.abc import AsyncIterator
from io import BytesIO
from typing import Any

from PIL import Image
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from app.db.models import InvoiceTemplate
from app.db.session import unit_of_work
from app.repositories.invoice_templates import InvoiceTemplateRepository
from app.schemas.invoice_templates import (
    InvoiceTemplateCreate,
    InvoiceTemplateRead,
    InvoiceTemplateUpdate,
)
from app.services import audit, policy
from app.services.avatar_files import inspect_avatar
from app.services.billing_common import conflict, now_utc
from app.services.members import integrity_constraint
from app.services.policy import OrgContext
from app.services.storage import StorageError, discard, get_storage, new_key

NAME_INDEX = "uq_invoice_templates_organization_id_name_lower"
DEFAULT_INDEX = "uq_invoice_templates_organization_id_default"
_AUDITED = (
    "name",
    "layout",
    "show_logo",
    "show_bank_details",
    "footer_text",
    "accent_color",
    "watermark_text",
    "status",
)


def name_taken_error() -> ConflictError:
    message = "A template with this name already exists."
    return ConflictError(
        message,
        code="TEMPLATE_NAME_TAKEN",
        details=[{"field": "name", "code": "duplicate", "message": message}],
    )


def text_and_image_error(field: str) -> ValidationError:
    return ValidationError.for_field(
        field, "A watermark is a text or a picture: remove the one the template has first."
    )


def _meta(template: InvoiceTemplate, **extra: Any) -> dict[str, Any]:
    return {
        "name": template.name,
        "layout": template.layout,
        "status": template.status,
        "is_default": template.is_default,
        **extra,
    }


class InvoiceTemplateService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.templates = InvoiceTemplateRepository(session)
        self.storage = get_storage()

    async def _audit(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        template: InvoiceTemplate,
        meta: dict[str, Any],
    ) -> None:
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=ctx.organization_id,
            target_type="invoice_template",
            target_id=template.id,
            meta=meta,
        )

    async def _read(self, ctx: OrgContext, template: InvoiceTemplate) -> InvoiceTemplateRead:
        counts = await self.templates.client_counts(ctx.organization_id, [template.id])
        return InvoiceTemplateRead.from_model(template, counts.get(template.id, 0))

    async def _get(
        self, ctx: OrgContext, template_id: uuid.UUID, *, lock: bool = False
    ) -> InvoiceTemplate:
        template = await self.templates.get(ctx.organization_id, template_id, lock=lock)
        if template is None:
            raise NotFoundError("Invoice template not found.")
        return template

    # --- Reads ----------------------------------------------------------------------------

    async def list(self, ctx: OrgContext, *, include_inactive: bool) -> list[InvoiceTemplateRead]:
        if include_inactive:
            policy.ensure(ctx, "settings.manage")
        templates = await self.templates.list(
            ctx.organization_id, include_inactive=include_inactive
        )
        counts = await self.templates.client_counts(ctx.organization_id, [t.id for t in templates])
        return [InvoiceTemplateRead.from_model(t, counts.get(t.id, 0)) for t in templates]

    async def get(self, ctx: OrgContext, template_id: uuid.UUID) -> InvoiceTemplateRead:
        template = await self._get(ctx, template_id)
        if template.status != "active" and not policy.has_permission(ctx, "settings.manage"):
            raise NotFoundError("Invoice template not found.")
        return await self._read(ctx, template)

    # --- Writes ---------------------------------------------------------------------------

    async def create(
        self, ctx: OrgContext, data: InvoiceTemplateCreate, rctx: RequestContext
    ) -> InvoiceTemplateRead:
        org_id = ctx.organization_id
        try:
            async with unit_of_work(self.session):
                if data.is_default:
                    await self.templates.lock_all(org_id)
                if await self.templates.name_taken(org_id, data.name):
                    raise name_taken_error()
                if data.is_default:
                    await self.templates.clear_default(org_id, None)
                template = await self.templates.add(
                    InvoiceTemplate(organization_id=org_id, **data.model_dump())
                )
                await self._audit(ctx, rctx, "invoice_template.created", template, _meta(template))
                result = await self._read(ctx, template)
        except IntegrityError as exc:
            if integrity_constraint(exc) == NAME_INDEX:
                raise name_taken_error() from exc
            raise
        return result

    async def update(
        self,
        ctx: OrgContext,
        template_id: uuid.UUID,
        data: InvoiceTemplateUpdate,
        rctx: RequestContext,
    ) -> InvoiceTemplateRead:
        values = data.model_dump(exclude_unset=True)
        try:
            async with unit_of_work(self.session):
                template = await self._get(ctx, template_id, lock=True)
                if values.get("status") == "inactive" and template.is_default:
                    raise self._default_error("deactivated")
                if values.get("watermark_text") and template.watermark_image_key:
                    raise text_and_image_error("watermark_text")
                renamed = "name" in values and values["name"].lower() != template.name.lower()
                if renamed and await self.templates.name_taken(
                    ctx.organization_id, values["name"], exclude_id=template.id
                ):
                    raise name_taken_error()
                changes: dict[str, list[Any]] = {}
                for field, value in values.items():
                    old = getattr(template, field)
                    if old != value:
                        changes[field] = [old, value]
                        setattr(template, field, value)
                template = await self.templates.save(template)
                if changes:
                    action = "invoice_template.updated"
                    if "status" in changes:
                        action = (
                            "invoice_template.deactivated"
                            if template.status == "inactive"
                            else "invoice_template.activated"
                        )
                    await self._audit(ctx, rctx, action, template, _meta(template, changes=changes))
                result = await self._read(ctx, template)
        except IntegrityError as exc:
            if integrity_constraint(exc) == NAME_INDEX:
                raise name_taken_error() from exc
            raise
        return result

    async def delete(self, ctx: OrgContext, template_id: uuid.UUID, rctx: RequestContext) -> None:
        async with unit_of_work(self.session):
            template = await self._get(ctx, template_id, lock=True)
            if template.is_default:
                raise self._default_error("deleted")
            used = (await self.templates.client_counts(ctx.organization_id, [template.id])).get(
                template.id, 0
            )
            if used:
                raise conflict(
                    "TEMPLATE_IN_USE",
                    "Clients use this template: deactivate it instead of deleting it.",
                    details={"clients": used},
                )
            template.deleted_at = now_utc()
            await self.templates.save(template)
            await self._audit(ctx, rctx, "invoice_template.deleted", template, _meta(template))

    async def make_default(
        self, ctx: OrgContext, template_id: uuid.UUID, rctx: RequestContext
    ) -> InvoiceTemplateRead:
        org_id = ctx.organization_id
        try:
            async with unit_of_work(self.session):
                # Every template, in id order: concurrent changes of the default queue up.
                await self.templates.lock_all(org_id)
                template = await self._get(ctx, template_id, lock=True)
                if template.status != "active":
                    raise conflict(
                        "TEMPLATE_INACTIVE", "An inactive template cannot be the default."
                    )
                if not template.is_default:
                    previous = await self.templates.get_default(org_id)
                    await self.templates.clear_default(org_id, template.id)
                    template.is_default = True
                    template = await self.templates.save(template)
                    await self._audit(
                        ctx,
                        rctx,
                        "invoice_template.made_default",
                        template,
                        _meta(
                            template,
                            previous_default_id=str(previous.id) if previous else None,
                            previous_default_name=previous.name if previous else None,
                        ),
                    )
                result = await self._read(ctx, template)
        except IntegrityError as exc:
            if integrity_constraint(exc) == DEFAULT_INDEX:
                raise conflict(
                    "TEMPLATE_DEFAULT_CHANGED", "The default template changed: try again."
                ) from exc
            raise
        return result

    async def set_watermark_image(
        self,
        ctx: OrgContext,
        template_id: uuid.UUID,
        *,
        file_name: str,
        declared_type: str | None,
        data: bytes,
        rctx: RequestContext,
    ) -> InvoiceTemplateRead:
        """Replace the watermark picture. As for a profile picture, the file is stored first and
        the old one removed only after the row points at the new one."""
        inspected = inspect_avatar(file_name, declared_type, data)
        try:
            # Decoded as the PDF will (fpdf2 reads pictures with Pillow): a picture that does not
            # decode would break every document drawn with the template.
            with Image.open(BytesIO(data)) as image:
                image.load()
        except Exception as exc:
            raise ValidationError.for_field("file", "The image cannot be read.") from exc
        org_id = ctx.organization_id
        key = new_key()
        try:
            await self.storage.put(org_id, key, data)
        except StorageError as exc:
            raise ServiceUnavailableError(
                "File storage is unavailable. Please try again shortly.", code="STORAGE_UNAVAILABLE"
            ) from exc
        previous: str | None = None
        try:
            async with unit_of_work(self.session):
                template = await self._get(ctx, template_id, lock=True)
                if template.watermark_text:
                    raise text_and_image_error("file")
                previous = template.watermark_image_key
                template.watermark_image_key = key
                template.watermark_image_type = inspected.content_type
                template = await self.templates.save(template)
                await self._audit(
                    ctx,
                    rctx,
                    "invoice_template.updated",
                    template,
                    _meta(template, changes={"watermark_image": [previous is not None, True]}),
                )
                result = await self._read(ctx, template)
        except BaseException:
            await discard(self.storage, org_id, [key])
            raise
        if previous:
            await discard(self.storage, org_id, [previous])
        return result

    async def clear_watermark_image(
        self, ctx: OrgContext, template_id: uuid.UUID, rctx: RequestContext
    ) -> InvoiceTemplateRead:
        """Remove the watermark picture; not an error when there is none."""
        async with unit_of_work(self.session):
            template = await self._get(ctx, template_id, lock=True)
            previous = template.watermark_image_key
            if previous:
                template.watermark_image_key = None
                template.watermark_image_type = None
                template = await self.templates.save(template)
                await self._audit(
                    ctx,
                    rctx,
                    "invoice_template.updated",
                    template,
                    _meta(template, changes={"watermark_image": [True, False]}),
                )
            result = await self._read(ctx, template)
        if previous:
            await discard(self.storage, ctx.organization_id, [previous])
        return result

    async def open_watermark_image(
        self, ctx: OrgContext, template_id: uuid.UUID
    ) -> tuple[str, AsyncIterator[bytes]]:
        """The content type and content of the watermark picture, to whoever may read the
        template."""
        template = await self._get(ctx, template_id)
        if template.status != "active" and not policy.has_permission(ctx, "settings.manage"):
            raise NotFoundError("Invoice template not found.")
        if template.watermark_image_key is None or template.watermark_image_type is None:
            raise NotFoundError("Picture not found.")
        try:
            content = await self.storage.open(ctx.organization_id, template.watermark_image_key)
        except StorageError as exc:
            raise NotFoundError("Picture not found.") from exc
        return template.watermark_image_type, content

    @staticmethod
    def _default_error(what: str) -> ConflictError:
        return conflict(
            "TEMPLATE_IS_DEFAULT",
            f"The default template cannot be {what}: make another template the default first.",
        )

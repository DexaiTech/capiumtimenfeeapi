"""Email templates: the built-in catalogue and each practice's own versions of it.

Reading and writing both need ``organization.update`` or ``settings.manage``. A practice's version
is validated (only the template's own merge fields as plain ``{{ field }}`` placeholders; no
statements, attribute access or calls; no scripts in the HTML) and rendered by the substitution
engine in ``app.services.template_engine``, which evaluates nothing. Every change is audited.
"""

import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import RequestContext
from app.core.exceptions import NotFoundError, ValidationError
from app.db.models import EmailTemplate
from app.db.session import unit_of_work
from app.repositories.email_templates import EmailTemplateRepository
from app.schemas.email_templates import (
    EmailTemplatePreview,
    EmailTemplatePreviewRequest,
    EmailTemplateRead,
    EmailTemplateWrite,
    MergeFieldRead,
)
from app.services import policy, template_engine
from app.services.common import record_audit
from app.services.notification_settings import ADMIN_PERMISSIONS
from app.services.policy import OrgContext
from app.services.template_catalogue import (
    MERGE_FIELDS,
    NOTIFICATION_KEYS,
    TEMPLATES,
    Audience,
    TemplateSpec,
    sample_values,
)
from app.services.template_render import TemplateContent, render_email

# The practice-facing system emails whose worker jobs look for a practice's version.
CUSTOMISABLE_SYSTEM_KEYS = frozenset(
    key for key, spec in TEMPLATES.items() if spec.category == "system"
)


def default_content(spec: TemplateSpec) -> TemplateContent:
    return TemplateContent(subject=spec.subject, html=spec.html, text=spec.text)


def content_of(row: EmailTemplate) -> TemplateContent:
    return TemplateContent(subject=row.subject, html=row.html_body, text=row.text_body)


async def load_override(
    session: AsyncSession, organization_id: uuid.UUID, key: str
) -> TemplateContent | None:
    """The practice's version of the template, or None when it uses the built-in one."""
    row = await EmailTemplateRepository(session).get(organization_id, key)
    return content_of(row) if row else None


def validate_content(spec: TemplateSpec, content: TemplateContent) -> None:
    """422 with a detail per problem: unknown fields, logic, unsafe HTML, a missing required
    field."""
    details: list[dict[str, str]] = []
    parts = (
        ("subject", content.subject, False),
        ("html_body", content.html, True),
        ("text_body", content.text, False),
    )
    for name, source, is_html in parts:
        problems = template_engine.check(source, spec.fields)
        if is_html:
            problems += template_engine.check_html(source)
        details += [{"field": name, "code": p.code, "message": p.message} for p in problems]
    if "\n" in content.subject or "\r" in content.subject:
        details.append(
            {
                "field": "subject",
                "code": "multiline_subject",
                "message": "The subject must be a single line.",
            }
        )
    for required in spec.required:
        for name, source in (("html_body", content.html), ("text_body", content.text)):
            if required not in template_engine.fields_used(source):
                details.append(
                    {
                        "field": name,
                        "code": "missing_merge_field",
                        "message": f"This template must include {{{{ {required} }}}}.",
                    }
                )
    if details:
        raise ValidationError(details=details)


def _spec(key: str) -> TemplateSpec:
    spec = TEMPLATES.get(key)
    if spec is None:
        raise NotFoundError("Template not found.")
    return spec


class EmailTemplateService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repo = EmailTemplateRepository(session)

    # --- Reads ---------------------------------------------------------------------------

    async def list_templates(
        self, ctx: OrgContext, audience: Audience | None = None
    ) -> list[EmailTemplateRead]:
        policy.ensure_any(ctx, *ADMIN_PERMISSIONS)
        rows = await self.repo.all(ctx.organization_id)
        names = await self.repo.user_names({r.updated_by for r in rows.values() if r.updated_by})
        return [
            self._view(spec, rows.get(spec.key), names)
            for spec in TEMPLATES.values()
            if audience is None or spec.audience == audience
        ]

    async def get(self, ctx: OrgContext, key: str) -> EmailTemplateRead:
        policy.ensure_any(ctx, *ADMIN_PERMISSIONS)
        spec = _spec(key)
        return await self._read(ctx, spec)

    # --- Writes --------------------------------------------------------------------------

    async def put(
        self, ctx: OrgContext, key: str, data: EmailTemplateWrite, rctx: RequestContext
    ) -> EmailTemplateRead:
        policy.ensure_any(ctx, *ADMIN_PERMISSIONS)
        spec = _spec(key)
        after = TemplateContent(subject=data.subject, html=data.html_body, text=data.text_body)
        validate_content(spec, after)
        async with unit_of_work(self.session):
            row = await self.repo.get(ctx.organization_id, key, for_update=True)
            before = content_of(row) if row else default_content(spec)
            if row is None or before != after:
                await self.repo.save(
                    ctx.organization_id,
                    key,
                    subject=after.subject,
                    html_body=after.html,
                    text_body=after.text,
                    updated_by=ctx.user.id,
                )
                saved = await self.repo.get(ctx.organization_id, key)
                assert saved is not None
                changed = [
                    name
                    for name, old, new in (
                        ("subject", before.subject, after.subject),
                        ("html_body", before.html, after.html),
                        ("text_body", before.text, after.text),
                    )
                    if old != new
                ]
                meta: dict[str, object] = {
                    "template_key": key,
                    "template_name": spec.name,
                    "created": row is None,
                    "changed_fields": changed,
                }
                if "subject" in changed:
                    meta["changes"] = {"subject": [before.subject, after.subject]}
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "email_template.updated",
                    "email_template",
                    saved.id,
                    meta,
                )
        return await self._read(ctx, spec)

    async def reset(self, ctx: OrgContext, key: str, rctx: RequestContext) -> EmailTemplateRead:
        """Back to the built-in template. Idempotent: nothing to undo is not an error."""
        policy.ensure_any(ctx, *ADMIN_PERMISSIONS)
        spec = _spec(key)
        async with unit_of_work(self.session):
            row = await self.repo.get(ctx.organization_id, key, for_update=True)
            if row is not None:
                row_id = row.id
                await self.repo.delete(row)
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "email_template.reset",
                    "email_template",
                    row_id,
                    {"template_key": key, "template_name": spec.name},
                )
        return await self._read(ctx, spec)

    async def preview(
        self, ctx: OrgContext, key: str, data: EmailTemplatePreviewRequest
    ) -> EmailTemplatePreview:
        """Render a draft (parts left out are the saved template's) with sample values."""
        policy.ensure_any(ctx, *ADMIN_PERMISSIONS)
        spec = _spec(key)
        unknown = [name for name in data.values if name not in spec.fields]
        if unknown:
            raise ValidationError(
                details=[
                    {
                        "field": f"values.{name}",
                        "code": "unknown_merge_field",
                        "message": f"{name} is not a merge field of this template.",
                    }
                    for name in unknown
                ]
            )
        row = await self.repo.get(ctx.organization_id, key)
        saved = content_of(row) if row else default_content(spec)
        draft = TemplateContent(
            subject=data.subject if data.subject is not None else saved.subject,
            html=data.html_body if data.html_body is not None else saved.html,
            text=data.text_body if data.text_body is not None else saved.text,
        )
        validate_content(spec, draft)
        values = self._sample(ctx, spec, data.values)
        rendered = render_email(
            draft, values, brand=ctx.organization.name, app_name=self.settings.app_name
        )
        return EmailTemplatePreview(
            subject=rendered.subject, html=rendered.html, text=rendered.text, values=values
        )

    # --- Helpers -------------------------------------------------------------------------

    def _sample(
        self, ctx: OrgContext, spec: TemplateSpec, supplied: Mapping[str, str]
    ) -> dict[str, str]:
        base = f"{self.settings.frontend_url}"
        values = sample_values(spec)
        real = {
            "recipient_name": ctx.user.full_name,
            "practice_name": ctx.organization.name,
            "app_name": self.settings.app_name,
            "login_url": f"{base}/login",
            "link": (
                f"{base}/{ctx.organization.slug}"
                if spec.key in NOTIFICATION_KEYS
                else f"{base}/reset-password?token=sample"
            ),
        }
        values.update({k: v for k, v in real.items() if k in values})
        values.update({k: v for k, v in supplied.items() if k in values})
        return values

    async def _read(self, ctx: OrgContext, spec: TemplateSpec) -> EmailTemplateRead:
        row = await self.repo.get(ctx.organization_id, spec.key)
        names = await self.repo.user_names([row.updated_by] if row and row.updated_by else [])
        return self._view(spec, row, names)

    @staticmethod
    def _view(
        spec: TemplateSpec, row: EmailTemplate | None, names: Mapping[uuid.UUID, str]
    ) -> EmailTemplateRead:
        content = content_of(row) if row else default_content(spec)
        updater = row.updated_by if row else None
        fields: Sequence[str] = spec.fields
        return EmailTemplateRead(
            key=spec.key,
            name=spec.name,
            description=spec.description,
            category=spec.category,
            audience=spec.audience,
            notification_type=spec.key if spec.key in NOTIFICATION_KEYS else None,
            merge_fields=[
                MergeFieldRead(
                    name=name,
                    label=MERGE_FIELDS[name].label,
                    description=MERGE_FIELDS[name].description,
                    sample=MERGE_FIELDS[name].sample,
                )
                for name in fields
            ],
            required_fields=list(spec.required),
            customised=row is not None,
            subject=content.subject,
            html_body=content.html,
            text_body=content.text,
            updated_at=row.updated_at if row else None,
            updated_by=updater,
            updated_by_name=names.get(updater) if updater else None,
        )

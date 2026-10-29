"""Email templates: the built-in catalogue and the practice's own versions. HTTP only."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependencies.common import ContextDep, SessionDep, SettingsDep
from app.dependencies.organization import OrgContextDep
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.email_templates import (
    EmailTemplatePreview,
    EmailTemplatePreviewRequest,
    EmailTemplateRead,
    EmailTemplateWrite,
)
from app.services.email_templates import EmailTemplateService
from app.services.template_catalogue import Audience

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Email templates"])


def get_template_service(session: SessionDep, settings: SettingsDep) -> EmailTemplateService:
    return EmailTemplateService(session, settings)


TemplateServiceDep = Annotated[EmailTemplateService, Depends(get_template_service)]

_ACCESS = " Requires `organization.update` or `settings.manage`."
_FIELDS = (
    "Only the template's own merge fields may be used, as plain `{{ field }}` placeholders: "
    "no statements (`{% %}`), attribute access, filters or calls, and no scripts, frames, "
    "forms or event handlers in the HTML (422 with a detail per problem). Nothing is "
    "evaluated, so a template can never run code or read a secret."
)


@router.get(
    "/email-templates",
    summary="List the email templates",
    description=(
        "Every template of the catalogue (the notification emails and the practice-facing "
        "system emails) with its effective subject and bodies: the practice's own version when "
        "`customised`, else the built-in one. Each lists its allowed merge fields and its "
        "`audience`: `external` for the emails sent to clients, `internal` for those sent to "
        "the team; `audience` filters the list." + _ACCESS
    ),
    responses=error_responses(403, 404),
)
async def list_email_templates(
    org: OrgContextDep,
    service: TemplateServiceDep,
    audience: Annotated[
        Audience | None,
        Query(description="Only the emails for clients (`external`) or the team (`internal`)"),
    ] = None,
) -> ApiResponse[list[EmailTemplateRead]]:
    return ok(await service.list_templates(org, audience))


@router.get(
    "/email-templates/{key}",
    summary="Get an email template",
    description="One template by key (for example `budget_alert`); 404 for an unknown key."
    + _ACCESS,
    responses=error_responses(403, 404),
)
async def get_email_template(
    key: str, org: OrgContextDep, service: TemplateServiceDep
) -> ApiResponse[EmailTemplateRead]:
    return ok(await service.get(org, key))


@router.put(
    "/email-templates/{key}",
    summary="Customise an email template",
    description=(
        "Saves the practice's version of the template (subject, HTML body, text body), "
        "replacing an earlier one. " + _FIELDS + " Audited as `email_template.updated`." + _ACCESS
    ),
    responses=error_responses(403, 404),
)
async def put_email_template(
    key: str,
    body: EmailTemplateWrite,
    org: OrgContextDep,
    service: TemplateServiceDep,
    ctx: ContextDep,
) -> ApiResponse[EmailTemplateRead]:
    return ok(await service.put(org, key, body, ctx), "Template saved")


@router.delete(
    "/email-templates/{key}",
    summary="Reset an email template to its default",
    description=(
        "Deletes the practice's version so the built-in template applies again; returns that "
        "template. Idempotent. Audited as `email_template.reset`." + _ACCESS
    ),
    responses=error_responses(403, 404),
)
async def reset_email_template(
    key: str, org: OrgContextDep, service: TemplateServiceDep, ctx: ContextDep
) -> ApiResponse[EmailTemplateRead]:
    return ok(await service.reset(org, key, ctx), "Template reset to the default")


@router.post(
    "/email-templates/{key}/preview",
    summary="Preview an email template",
    description=(
        "Renders a template without saving or sending anything and returns the subject, the "
        "complete HTML email and the text body. The body may carry a draft (`subject`, "
        "`html_body`, `text_body`; parts left out are the saved template's) and `values` for "
        "merge fields (the others show their sample). The draft is validated like a save. "
        + _FIELDS
        + _ACCESS
    ),
    responses=error_responses(403, 404),
)
async def preview_email_template(
    key: str,
    org: OrgContextDep,
    service: TemplateServiceDep,
    body: EmailTemplatePreviewRequest | None = None,
) -> ApiResponse[EmailTemplatePreview]:
    return ok(await service.preview(org, key, body or EmailTemplatePreviewRequest()))

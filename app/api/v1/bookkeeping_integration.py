"""The bookkeeping mappings: which income account each service is booked to and which tax code each VAT
code is booked to. HTTP only; the rules are in ``app/services/bookkeeping_mappings.py``."""

from fastapi import APIRouter

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.integrations import IntegrationManager, IntegrationReader
from app.schemas.bookkeeping_mappings import MappingsRead, MappingsWrite
from app.schemas.common import ApiResponse, error_responses, ok
from app.services.bookkeeping_mappings import MappingService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Integrations"])


@router.get(
    "/integrations/bookkeeping/mappings",
    summary="Get the bookkeeping mappings",
    description=(
        "Every service of the practice with the income account it is booked to, every VAT code with "
        "its tax code, and one row each for invoice lines that have no service and for lines that "
        "carry no VAT code (`service_id` / `vat_rate_id` null). A row with a null `account_code` "
        "or `tax_code` is not mapped; `unmapped_services` and `unmapped_vat_rates` count them. "
        "A document with a line that has an unmapped service or VAT code is not sent: its sync "
        "item fails with `UNMAPPED` and says which. Requires `integration.read`."
    ),
    responses=error_responses(403, 404),
)
async def get_mappings(org: IntegrationReader, session: SessionDep) -> ApiResponse[MappingsRead]:
    return ok(await MappingService(session).get(org))


@router.put(
    "/integrations/bookkeeping/mappings",
    summary="Replace the bookkeeping mappings",
    description=(
        "Writes the complete set of mappings: whatever is not listed becomes unmapped. Each "
        "service and each VAT code can be listed once (`service_id` / `vat_rate_id` null is the "
        "row for lines without one); another practice's ids and unknown ids answer 422 naming the "
        "row. Codes are letters, digits and `. _ / -`, 1 to 20 characters. Adding a missing "
        "mapping does not resend anything by itself: retry the failed items. Audited with what "
        "was added, removed or changed. Requires `integration.manage`."
    ),
    responses=error_responses(403, 404),
)
async def put_mappings(
    body: MappingsWrite, org: IntegrationManager, session: SessionDep, ctx: ContextDep
) -> ApiResponse[MappingsRead]:
    return ok(await MappingService(session).put(org, body, ctx), "Mappings saved")

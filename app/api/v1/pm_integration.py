"""The practice management integration (Phase 2A): import and link jobs and tasks, receive PM
events, unlink a job, and the mock PM system's change feed. HTTP only; the rules are in
``app/services/pm_sync.py``."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Path, status

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.integrations import IntegrationManager
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.integration_events import (
    EventResult,
    MockChanged,
    PmEventRequest,
    PmImportRequest,
    PmImportResult,
    PmMockChange,
    ProcessedFeed,
)
from app.schemas.jobs import JobRead
from app.services.integration_connections import ConnectionService
from app.services.integrations import (
    PRACTICE_MANAGEMENT,
    MockPracticeManagementGateway,
    PmEvent,
    get_practice_management_gateway,
)
from app.services.integrations.practice_management import (
    PM_EDITABLE_FIELDS,
    PM_JOB_STATUSES,
    PM_TASK_STATUSES,
)
from app.services.pm_sync import PmSyncService
from app.services.time_jobs import JobService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Integrations"])

JobIdPath = Annotated[uuid.UUID, Path(description="The job's id")]


def _mock_gateway() -> MockPracticeManagementGateway:
    gateway = get_practice_management_gateway()
    if not isinstance(gateway, MockPracticeManagementGateway) or get_settings().is_production:
        raise NotFoundError("Not found.")
    return gateway


@router.post(
    "/integrations/practice-management/import-jobs",
    summary="Import and link jobs from practice management",
    description=(
        "Reads practice management's jobs (with their tasks) and, for each, creates the job, "
        "links an existing job of the same client and name, or updates the linked job, matched "
        "by `pm_job_id`: **repeating it changes nothing**. A job is imported for a client the "
        "practice has enabled from the central client service (practice management names the "
        "client by its central id); other jobs are `skipped` with the reason. The name, "
        "service, manager, partner and dates of a linked job come from practice management "
        "(refused on edit: 409 `JOB_FIELD_MANAGED_BY_PM`); its tasks are matched by `pm_task_id` "
        "(or linked by title); statuses are applied through the same transition rules as a "
        "change made here, and a status those rules refuse is reported as a warning. At most "
        "`limit` jobs per call: while `has_more` call again with `next_cursor`. While practice "
        "management is down the answer is 200 with `available: false`. 409 "
        "`INTEGRATION_DISABLED` while the integration is off. Requires `integration.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def import_jobs(
    org: IntegrationManager,
    session: SessionDep,
    ctx: ContextDep,
    body: PmImportRequest | None = None,
) -> ApiResponse[PmImportResult]:
    await ConnectionService(session).ensure_enabled(org.organization_id, PRACTICE_MANAGEMENT)
    request = body or PmImportRequest()
    service = PmSyncService(session, get_practice_management_gateway())
    result = await service.import_jobs(org, ctx, cursor=request.cursor, limit=request.limit)
    return ok(result, "Jobs imported")


@router.post(
    "/integrations/practice-management/events",
    summary="Receive a practice management event",
    description=(
        "One event from practice management: `job_updated` (the job is read again and its "
        "PM-owned fields, tasks and status are applied), `job_status_changed` or "
        "`task_status_changed` (with `status`). It is applied to the linked job or task through "
        "the same transition rules as a change made here; a change those rules refuse is "
        "`failed` and kept as a failed sync item (retry it from the sync items). An event for "
        "a job that is not linked here is `skipped`, never an error. Idempotent by `event_id`: "
        "a repeat is logged `skipped` and changes nothing. A status change that came from "
        "practice management is not sent back to it. 409 `INTEGRATION_DISABLED` while the "
        "integration is off. Requires `integration.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def receive_event(
    body: PmEventRequest, org: IntegrationManager, session: SessionDep, ctx: ContextDep
) -> ApiResponse[EventResult]:
    await ConnectionService(session).ensure_enabled(org.organization_id, PRACTICE_MANAGEMENT)
    event = PmEvent(
        event_id=body.event_id,
        type=body.type,
        pm_job_id=body.pm_job_id,
        occurred_at=body.occurred_at or datetime.now(UTC),
        pm_task_id=body.pm_task_id,
        status=body.status,
    )
    service = PmSyncService(session, get_practice_management_gateway())
    handled = await service.handle_event(org, ctx, event, source="webhook")
    return ok(EventResult.from_handled(handled), "Event processed")


@router.post(
    "/integrations/practice-management/process-changes",
    summary="Pull and apply the practice management change feed",
    description=(
        "Reads the events after this practice's cursor from practice management and applies each "
        "through the same handler as `POST /integrations/practice-management/events` (at most "
        "100 per call; `has_more` says to call again). The cursor moves forward only when the "
        "whole batch was handled; an event that a rule refuses is kept as a failed sync item "
        "and does not stop the feed. While practice management is down the answer is 200 with "
        "`available: false`. 409 `INTEGRATION_DISABLED` while the integration is off. Requires "
        "`integration.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def process_changes(
    org: IntegrationManager, session: SessionDep, ctx: ContextDep
) -> ApiResponse[ProcessedFeed]:
    await ConnectionService(session).ensure_enabled(org.organization_id, PRACTICE_MANAGEMENT)
    service = PmSyncService(session, get_practice_management_gateway())
    return ok(
        ProcessedFeed.from_polled(await service.process_changes(org, ctx)), "Changes processed"
    )


@router.post(
    "/integrations/practice-management/mock/changes",
    status_code=status.HTTP_201_CREATED,
    summary="Make a change in the mock practice management system",
    description=(
        "Changes a job in the mock practice management system and appends the event to its "
        "change feed, which `process-changes` then reads: `job_updated` (with any of `name`, "
        "`service_code`, `manager_email`, `partner_email`, `start_date`, `due_date`), "
        "`job_status_changed` or `task_status_changed` (with `status`). 404 for an unknown job "
        "or task, and unless the gateway is the mock and `APP_ENV` is not production. Requires "
        "`integration.manage`."
    ),
    responses=error_responses(403, 404),
)
async def push_mock_change(body: PmMockChange, org: IntegrationManager) -> ApiResponse[MockChanged]:
    gateway = _mock_gateway()
    try:
        if body.type == "job_updated":
            fields = {
                name: getattr(body, name)
                for name in PM_EDITABLE_FIELDS
                if name in body.model_fields_set
            }
            if not fields:
                raise ValidationError.for_field("type", "A job update changes at least one field.")
            event = gateway.update_job(body.pm_job_id, **fields)
        elif body.type == "job_status_changed":
            if body.status not in PM_JOB_STATUSES:
                raise ValidationError.for_field("status", "Not a practice management job status.")
            event = gateway.change_job_status(body.pm_job_id, body.status)
        else:
            if body.pm_task_id is None or body.status not in PM_TASK_STATUSES:
                raise ValidationError.for_field(
                    "status", "A task change names the task and one of its statuses."
                )
            event = gateway.change_task_status(body.pm_job_id, body.pm_task_id, body.status)
    except KeyError as exc:
        raise NotFoundError("Practice management job or task not found.") from exc
    return ok(MockChanged(event_id=event.event_id, type=event.type), "Change pushed")


@router.delete(
    "/jobs/{job_id}/pm-link",
    summary="Unlink a job from practice management",
    description=(
        "Makes a linked job a local job again: its name, service, manager, partner and dates "
        "become editable, its tasks are unlinked and what was queued for practice management is "
        "dropped. Importing again links the same job again (a job of the same client and name "
        "is linked, never duplicated). 409 `JOB_NOT_LINKED` for a job that is not linked. "
        "Requires `integration.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def unlink_job(
    job_id: JobIdPath, org: IntegrationManager, session: SessionDep, ctx: ContextDep
) -> ApiResponse[JobRead]:
    await PmSyncService(session, get_practice_management_gateway()).unlink(org, job_id, ctx)
    return ok(await JobService(session).get(org, job_id), "Job unlinked")

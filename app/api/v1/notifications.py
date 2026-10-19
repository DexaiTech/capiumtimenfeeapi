"""The signed-in person's notifications and notification settings. HTTP only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, PageParamsDep
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.notifications import (
    MarkedRead,
    NotificationRead,
    NotificationSettingsRead,
    NotificationSettingsUpdate,
    PracticeNotificationSettingsRead,
    PracticeNotificationSettingsUpdate,
    UnreadCount,
)
from app.services.notification_settings import NotificationSettingsService
from app.services.notifications import NotificationService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Notifications"])


def get_notification_service(session: SessionDep) -> NotificationService:
    return NotificationService(session)


def get_settings_service(session: SessionDep) -> NotificationSettingsService:
    return NotificationSettingsService(session)


NotificationServiceDep = Annotated[NotificationService, Depends(get_notification_service)]
SettingsServiceDep = Annotated[NotificationSettingsService, Depends(get_settings_service)]


@router.get(
    "/notifications",
    summary="List my notifications",
    description=(
        "The caller's own notifications, newest first, cursor-paginated. `unread=true` lists "
        "only the unread ones. Any member may call it; nobody sees anyone else's."
    ),
    responses=error_responses(404),
)
async def list_notifications(
    org: OrgContextDep,
    service: NotificationServiceDep,
    page: PageParamsDep,
    unread: Annotated[bool, Query(description="Only notifications that are not read yet")] = False,
) -> ApiResponse[list[NotificationRead]]:
    result = await service.list_own(org, unread_only=unread, params=page)
    return ok_page([NotificationRead.from_model(row) for row in result.items], result.next_cursor)


@router.get(
    "/notifications/unread-count",
    summary="Count my unread notifications",
    description="How many of the caller's notifications are unread: the number on the bell.",
    responses=error_responses(404),
)
async def unread_notification_count(
    org: OrgContextDep, service: NotificationServiceDep
) -> ApiResponse[UnreadCount]:
    return ok(UnreadCount(count=await service.unread_count(org)))


@router.post(
    "/notifications/read-all",
    summary="Mark all my notifications as read",
    description="Marks every unread notification of the caller as read; `updated` counts them.",
    responses=error_responses(404),
)
async def read_all_notifications(
    org: OrgContextDep, service: NotificationServiceDep
) -> ApiResponse[MarkedRead]:
    return ok(MarkedRead(updated=await service.mark_all_read(org)), "Notifications marked as read")


@router.post(
    "/notifications/{notification_id}/read",
    summary="Mark a notification as read",
    description=(
        "Marks one of the caller's notifications as read. Idempotent: an already read one "
        "keeps its `read_at`. Another person's notification, and another practice's, are 404."
    ),
    responses=error_responses(404),
)
async def read_notification(
    notification_id: uuid.UUID, org: OrgContextDep, service: NotificationServiceDep
) -> ApiResponse[NotificationRead]:
    return ok(NotificationRead.from_model(await service.mark_read(org, notification_id)))


@router.get(
    "/notification-settings",
    summary="Get my notification settings",
    description=(
        "The caller's channel choices (in-app, email, SMS) for every notification type, with "
        "the practice's defaults where they have not chosen (`customised` is false), whether the "
        "practice has the type switched on (`enabled`) and the mobile number SMS goes to."
    ),
    responses=error_responses(404),
)
async def get_my_notification_settings(
    org: OrgContextDep, service: SettingsServiceDep
) -> ApiResponse[NotificationSettingsRead]:
    return ok(await service.get_own(org))


@router.put(
    "/notification-settings",
    summary="Update my notification settings",
    description=(
        "Saves the caller's channel choices for the listed types (the others keep theirs) and, "
        "when `sms_number` is sent, their mobile number (7-15 digits, optional leading +; null "
        "or blank clears it). Turning SMS on needs a number (422). A type the practice has "
        "switched off is never sent whatever the choice."
    ),
    responses=error_responses(404),
)
async def update_my_notification_settings(
    body: NotificationSettingsUpdate, org: OrgContextDep, service: SettingsServiceDep
) -> ApiResponse[NotificationSettingsRead]:
    return ok(await service.update_own(org, body), "Notification settings saved")


@router.get(
    "/notification-settings/practice",
    summary="Get the practice's notification settings",
    description=(
        "For every notification type whether the practice has it switched on and the channels "
        "people get until they choose their own. Requires `organization.update` or "
        "`settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def get_practice_notification_settings(
    org: OrgContextDep, service: SettingsServiceDep
) -> ApiResponse[PracticeNotificationSettingsRead]:
    return ok(await service.get_practice(org))


@router.put(
    "/notification-settings/practice",
    summary="Update the practice's notification settings",
    description=(
        "Switches the listed types on or off for the whole practice and sets their default "
        "channels (the others keep theirs). A type that is off sends nothing, in-app included, "
        "whatever people prefer. Audited. Requires `organization.update` or `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def update_practice_notification_settings(
    body: PracticeNotificationSettingsUpdate,
    org: OrgContextDep,
    service: SettingsServiceDep,
    ctx: ContextDep,
) -> ApiResponse[PracticeNotificationSettingsRead]:
    return ok(await service.update_practice(org, body, ctx), "Notification settings saved")

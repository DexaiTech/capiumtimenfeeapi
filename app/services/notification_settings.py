"""Notification settings: a person's own channel choices and the practice's switches.

What reaches a person for a type, per channel:

1. nothing at all when the practice has the type switched off;
2. otherwise the person's own choice, if they made one;
3. otherwise the practice's default for the type, if it set one;
4. otherwise the catalogue default (in-app and email on, SMS off).

Own settings are open to every member. The practice's switches need ``organization.update`` or
``settings.manage``.
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ValidationError
from app.core.notification_types import BY_CODE, CATALOGUE, NotificationTypeDef
from app.db.models import NotificationPreference, NotificationSetting
from app.db.session import unit_of_work
from app.repositories.notifications import NotificationSettingsRepository
from app.schemas.notifications import (
    NotificationPreferenceRead,
    NotificationSettingsRead,
    NotificationSettingsUpdate,
    PracticeNotificationSettingsRead,
    PracticeNotificationSettingsUpdate,
    PracticeTypeRead,
)
from app.services import policy
from app.services.common import record_audit
from app.services.policy import OrgContext

ADMIN_PERMISSIONS = ("organization.update", "settings.manage")


@dataclass(frozen=True, slots=True)
class Channels:
    in_app: bool
    email: bool
    sms: bool


def catalogue_channels(spec: NotificationTypeDef) -> Channels:
    return Channels(spec.in_app, spec.email, spec.sms)


def practice_channels(spec: NotificationTypeDef, setting: NotificationSetting | None) -> Channels:
    """The channels a person without a choice of their own gets."""
    if setting is None:
        return catalogue_channels(spec)
    return Channels(setting.default_in_app, setting.default_email, setting.default_sms)


def effective_channels(
    spec: NotificationTypeDef,
    setting: NotificationSetting | None,
    preference: NotificationPreference | None,
) -> Channels:
    if preference is not None:
        return Channels(preference.in_app, preference.email, preference.sms)
    return practice_channels(spec, setting)


def type_enabled(setting: NotificationSetting | None) -> bool:
    return setting is None or setting.enabled


class NotificationSettingsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = NotificationSettingsRepository(session)

    # --- The person's own settings -------------------------------------------------------

    async def get_own(self, ctx: OrgContext) -> NotificationSettingsRead:
        practice = await self.repo.practice_settings(ctx.organization_id)
        own = await self.repo.user_preferences(ctx.organization_id, ctx.user.id)
        rows = []
        for spec in CATALOGUE:
            setting, preference = practice.get(spec.code), own.get(spec.code)
            channels = effective_channels(spec, setting, preference)
            rows.append(
                NotificationPreferenceRead(
                    type=spec.code,
                    label=spec.label,
                    description=spec.description,
                    enabled=type_enabled(setting),
                    in_app=channels.in_app,
                    email=channels.email,
                    sms=channels.sms,
                    customised=preference is not None,
                )
            )
        member = await self.repo.member_mobile_number(ctx.organization_id, ctx.user.id)
        return NotificationSettingsRead(sms_number=member, preferences=rows)

    async def update_own(
        self, ctx: OrgContext, data: NotificationSettingsUpdate
    ) -> NotificationSettingsRead:
        number_given = "sms_number" in data.model_fields_set
        number = (
            data.sms_number
            if number_given
            else await self.repo.member_mobile_number(ctx.organization_id, ctx.user.id)
        )
        if number is None and any(item.sms for item in data.preferences):
            raise ValidationError.for_field(
                "sms_number", "Add a mobile number to receive SMS notifications.", "required"
            )
        async with unit_of_work(self.session):
            for item in data.preferences:
                await self.repo.save_preference(
                    ctx.organization_id,
                    ctx.user.id,
                    item.type,
                    in_app=item.in_app,
                    email=item.email,
                    sms=item.sms,
                )
            if number_given:
                await self.repo.set_mobile_number(ctx.organization_id, ctx.user.id, number)
        return await self.get_own(ctx)

    # --- The practice's settings ---------------------------------------------------------

    async def get_practice(self, ctx: OrgContext) -> PracticeNotificationSettingsRead:
        policy.ensure_any(ctx, *ADMIN_PERMISSIONS)
        practice = await self.repo.practice_settings(ctx.organization_id)
        rows = []
        for spec in CATALOGUE:
            setting = practice.get(spec.code)
            channels = practice_channels(spec, setting)
            rows.append(
                PracticeTypeRead(
                    type=spec.code,
                    label=spec.label,
                    description=spec.description,
                    enabled=type_enabled(setting),
                    in_app=channels.in_app,
                    email=channels.email,
                    sms=channels.sms,
                )
            )
        return PracticeNotificationSettingsRead(types=rows)

    async def update_practice(
        self, ctx: OrgContext, data: PracticeNotificationSettingsUpdate, rctx: RequestContext
    ) -> PracticeNotificationSettingsRead:
        policy.ensure_any(ctx, *ADMIN_PERMISSIONS)
        async with unit_of_work(self.session):
            practice = await self.repo.practice_settings(ctx.organization_id)
            changes: dict[str, dict[str, list[bool]]] = {}
            for item in data.types:
                spec = BY_CODE[item.type]
                setting = practice.get(item.type)
                old = practice_channels(spec, setting)
                new = {
                    "enabled": item.enabled,
                    "in_app": item.in_app,
                    "email": item.email,
                    "sms": item.sms,
                }
                before = {
                    "enabled": type_enabled(setting),
                    "in_app": old.in_app,
                    "email": old.email,
                    "sms": old.sms,
                }
                diff = {k: [before[k], v] for k, v in new.items() if before[k] != v}
                if not diff:
                    continue
                changes[item.type] = diff
                await self.repo.save_practice_setting(
                    ctx.organization_id,
                    item.type,
                    enabled=item.enabled,
                    in_app=item.in_app,
                    email=item.email,
                    sms=item.sms,
                    updated_by=ctx.user.id,
                )
            if changes:
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "notification_settings.updated",
                    "notification_settings",
                    None,
                    {"changes": changes},
                )
        return await self.get_practice(ctx)

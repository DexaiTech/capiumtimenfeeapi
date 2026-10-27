"""Notification queries. Every method is scoped to one practice, and the inbox to one person."""

import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    MemberPermissionOverride,
    Notification,
    NotificationPreference,
    NotificationSetting,
    OrganizationMember,
    Permission,
    RolePermission,
    User,
)
from app.utils.pagination import Page, PageParams, paginate


@dataclass(frozen=True, slots=True)
class Recipient:
    """A person who can be notified: an active member with a login."""

    user_id: uuid.UUID
    full_name: str
    email: str
    mobile_number: str | None


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- The inbox (own notifications only) ----------------------------------------------

    async def list_page(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        unread_only: bool,
        params: PageParams,
    ) -> Page[Notification]:
        """Newest first."""
        stmt = select(Notification).where(
            Notification.organization_id == organization_id, Notification.user_id == user_id
        )
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        return await paginate(
            self.session,
            stmt,
            sort_column=Notification.created_at,
            id_column=Notification.id,
            params=params,
            descending=True,
        )

    async def unread_count(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.organization_id == organization_id,
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def get(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, notification_id: uuid.UUID
    ) -> Notification | None:
        stmt = (
            select(Notification)
            .where(
                Notification.organization_id == organization_id,
                Notification.user_id == user_id,
                Notification.id == notification_id,
            )
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def mark_all_read(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> int:
        result = await self.session.execute(
            update(Notification)
            .where(
                Notification.organization_id == organization_id,
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
            )
            .values(read_at=datetime.now(UTC))
        )
        return int(getattr(result, "rowcount", 0) or 0)

    # --- Creating ------------------------------------------------------------------------

    async def insert_new(self, rows: Sequence[dict[str, Any]]) -> dict[uuid.UUID, uuid.UUID]:
        """Insert the rows that are not duplicates (same practice, person and ``dedupe_key``).

        Returns ``{user_id: notification_id}`` of the rows that were inserted.
        """
        if not rows:
            return {}
        stmt = (
            insert(Notification)
            .values(list(rows))
            .on_conflict_do_nothing()
            .returning(Notification.user_id, Notification.id)
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}


class NotificationSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def recipients(
        self, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, Recipient]:
        """The people among ``user_ids`` who can be notified."""
        if not user_ids:
            return {}
        stmt = (
            select(
                OrganizationMember.user_id,
                User.full_name,
                User.email,
                OrganizationMember.mobile_number,
            )
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id.in_(list(user_ids)),
                OrganizationMember.status == "active",
                OrganizationMember.login_enabled.is_(True),
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        return {
            row[0]: Recipient(user_id=row[0], full_name=row[1], email=row[2], mobile_number=row[3])
            for row in (await self.session.execute(stmt)).all()
        }

    async def practice_setting(
        self, organization_id: uuid.UUID, type_: str
    ) -> NotificationSetting | None:
        stmt = select(NotificationSetting).where(
            NotificationSetting.organization_id == organization_id,
            NotificationSetting.type == type_,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def practice_settings(self, organization_id: uuid.UUID) -> dict[str, NotificationSetting]:
        stmt = select(NotificationSetting).where(
            NotificationSetting.organization_id == organization_id
        )
        return {row.type: row for row in (await self.session.execute(stmt)).scalars()}

    async def preferences_for_type(
        self, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID], type_: str
    ) -> dict[uuid.UUID, NotificationPreference]:
        if not user_ids:
            return {}
        stmt = select(NotificationPreference).where(
            NotificationPreference.organization_id == organization_id,
            NotificationPreference.user_id.in_(list(user_ids)),
            NotificationPreference.type == type_,
        )
        return {row.user_id: row for row in (await self.session.execute(stmt)).scalars()}

    async def user_preferences(
        self, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> dict[str, NotificationPreference]:
        stmt = select(NotificationPreference).where(
            NotificationPreference.organization_id == organization_id,
            NotificationPreference.user_id == user_id,
        )
        return {row.type: row for row in (await self.session.execute(stmt)).scalars()}

    async def save_preference(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        type_: str,
        *,
        in_app: bool,
        email: bool,
        sms: bool,
    ) -> None:
        stmt = insert(NotificationPreference).values(
            organization_id=organization_id,
            user_id=user_id,
            type=type_,
            in_app=in_app,
            email=email,
            sms=sms,
        )
        await self.session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_notification_preferences_organization_id_user_id_type",
                set_={"in_app": in_app, "email": email, "sms": sms},
            )
        )

    async def save_practice_setting(
        self,
        organization_id: uuid.UUID,
        type_: str,
        *,
        enabled: bool,
        in_app: bool,
        email: bool,
        sms: bool,
        updated_by: uuid.UUID,
    ) -> None:
        values = {
            "enabled": enabled,
            "default_in_app": in_app,
            "default_email": email,
            "default_sms": sms,
            "updated_by": updated_by,
        }
        stmt = insert(NotificationSetting).values(
            organization_id=organization_id, type=type_, **values
        )
        await self.session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_notification_settings_organization_id_type", set_=values
            )
        )

    async def member_mobile_number(
        self, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> str | None:
        stmt = select(OrganizationMember.mobile_number).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def set_mobile_number(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, number: str | None
    ) -> None:
        await self.session.execute(
            update(OrganizationMember)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
            )
            .values(mobile_number=number)
        )

    async def members_with_any_permission(
        self,
        organization_id: uuid.UUID,
        codes: Iterable[str],
        *,
        among: Collection[uuid.UUID] | None = None,
    ) -> list[uuid.UUID]:
        """Active members who can sign in and hold at least one of the permissions: their role's,
        plus what was granted to them, less what was denied them."""
        wanted = list(codes)

        def override(effect: str, code_column):
            return (
                select(MemberPermissionOverride.id)
                .where(
                    MemberPermissionOverride.organization_id == OrganizationMember.organization_id,
                    MemberPermissionOverride.user_id == OrganizationMember.user_id,
                    MemberPermissionOverride.permission_code == code_column,
                    MemberPermissionOverride.effect == effect,
                )
                .correlate(OrganizationMember, Permission)
                .exists()
            )

        from_role = exists().where(
            RolePermission.role_id == OrganizationMember.role_id,
            Permission.id == RolePermission.permission_id,
            Permission.code.in_(wanted),
            ~override("deny", Permission.code),
        )
        granted = exists().where(
            MemberPermissionOverride.organization_id == OrganizationMember.organization_id,
            MemberPermissionOverride.user_id == OrganizationMember.user_id,
            MemberPermissionOverride.permission_code.in_(wanted),
            MemberPermissionOverride.effect == "grant",
        )
        stmt = select(OrganizationMember.user_id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.status == "active",
            OrganizationMember.login_enabled.is_(True),
            or_(from_role, granted),
        )
        if among is not None:
            stmt = stmt.where(OrganizationMember.user_id.in_(list(among)))
        return sorted((await self.session.execute(stmt)).scalars())

    async def manager_of(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID | None:
        stmt = select(OrganizationMember.manager_user_id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

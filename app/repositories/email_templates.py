"""Email template queries: a practice's overrides of the built-in templates."""

import uuid
from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EmailTemplate, User
from app.utils.ids import new_id


class EmailTemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def all(self, organization_id: uuid.UUID) -> dict[str, EmailTemplate]:
        stmt = select(EmailTemplate).where(EmailTemplate.organization_id == organization_id)
        return {row.template_key: row for row in (await self.session.execute(stmt)).scalars()}

    async def get(
        self, organization_id: uuid.UUID, key: str, *, for_update: bool = False
    ) -> EmailTemplate | None:
        stmt = (
            select(EmailTemplate)
            .where(
                EmailTemplate.organization_id == organization_id,
                EmailTemplate.template_key == key,
            )
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def save(
        self,
        organization_id: uuid.UUID,
        key: str,
        *,
        subject: str,
        html_body: str,
        text_body: str,
        updated_by: uuid.UUID,
    ) -> None:
        """Create or replace the practice's version (two first saves cannot collide)."""
        values = {
            "subject": subject,
            "html_body": html_body,
            "text_body": text_body,
            "updated_by": updated_by,
        }
        stmt = insert(EmailTemplate).values(
            id=new_id(), organization_id=organization_id, template_key=key, **values
        )
        await self.session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_email_templates_organization_id_template_key", set_=values
            )
        )

    async def delete(self, template: EmailTemplate) -> None:
        await self.session.delete(template)
        await self.session.flush()

    async def user_names(self, user_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not user_ids:
            return {}
        stmt = select(User.id, User.full_name).where(User.id.in_(list(user_ids)))
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

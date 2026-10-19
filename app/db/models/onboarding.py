"""Onboarding checklist: the decisions people made by hand.

The checklist itself (its steps, and what the practice's data says about each) is code and live
queries (``app/services/onboarding.py``). Only a person's decision is stored: one row per
practice and step, ``done`` or ``skipped``; ``todo`` takes the decision back, so the data speaks
for the step again.
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin

ONBOARDING_STATES = ("done", "skipped", "todo")


class OnboardingProgress(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A step somebody marked done or skipped (or took back with ``todo``)."""

    __tablename__ = "onboarding_progress"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "step_key", name="uq_onboarding_progress_organization_id_step_key"
        ),
        CheckConstraint("state IN ('done', 'skipped', 'todo')", name="state"),
        CheckConstraint("step_key ~ '^[a-z][a-z_]{0,39}$'", name="step_key_format"),
    )

    step_key: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    # Who made the decision; kept null when that account is deleted.
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


__all__ = ["ONBOARDING_STATES", "OnboardingProgress"]

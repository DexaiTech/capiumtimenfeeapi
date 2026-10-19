"""Who sees what of a time entry beyond the rows they may read at all."""

import uuid

from app.services.policy import OrgContext, has_permission


def can_see_note(ctx: OrgContext, owner_id: uuid.UUID) -> bool:
    """The internal note is for the owner and people who read or manage all time."""
    return (
        owner_id == ctx.user.id
        or has_permission(ctx, "time.read_all")
        or has_permission(ctx, "time.manage_all")
    )

"""One action on several staff members at once (docs/modules/team-rates.md, "Bulk actions").

Every person goes through the very same service call as the single endpoint (``update``,
``deactivate``, ``reactivate``, ``delete``), so each rule of the single action holds here too:
the last administrator, an owner changed by a non-owner, nobody deactivating themselves, a
person that records refer to who cannot be deleted. The whole batch is one transaction: each
person is tried inside a savepoint, the refusals are collected, and when there is any the batch
answers 409 ``BULK_REFUSED`` listing every person and reason, with nothing changed.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AppException, ConflictError, NotFoundError
from app.db.session import unit_of_work
from app.repositories.staff import StaffRow, normalised_status
from app.schemas.staff import StaffBulkAction, StaffBulkResult, StaffDeactivate, StaffUpdate
from app.services import policy
from app.services.common import record_audit
from app.services.policy import OrgContext
from app.services.staff import StaffService

_AUDIT_CODES_LIMIT = 100


def _state(row: StaffRow) -> tuple[Any, ...]:
    member = row.member
    return (
        member.grade_id,
        member.manager_user_id,
        normalised_status(member.status),
        member.leaving_date,
    )


class StaffBulkService:
    def __init__(self, session: AsyncSession, staff: StaffService) -> None:
        self.session = session
        self.staff = staff

    async def run(
        self, ctx: OrgContext, data: StaffBulkAction, rctx: RequestContext
    ) -> StaffBulkResult:
        policy.ensure(
            ctx, "member.remove" if data.action in ("deactivate", "delete") else "member.update"
        )
        ids = list(dict.fromkeys(data.staff_ids))
        async with unit_of_work(self.session):
            before: dict[uuid.UUID, StaffRow] = {}
            missing: list[uuid.UUID] = []
            for staff_id in ids:
                try:
                    before[staff_id] = await self.staff.fetch_row(ctx, staff_id)
                except NotFoundError:
                    missing.append(staff_id)
            if missing:
                raise NotFoundError(
                    "Some of the staff were not found.",
                    details={"staff_ids": [str(i) for i in missing]},
                )
            states = {i: _state(row) for i, row in before.items()}
            labels = {i: (row.member.staff_code, row.user.full_name) for i, row in before.items()}

            refused: list[dict[str, Any]] = []
            for staff_id in ids:
                try:
                    async with self.session.begin_nested():
                        await self._one(ctx, data, staff_id, rctx)
                except AppException as exc:
                    code, name = labels[staff_id]
                    refused.append(
                        {
                            "staff_id": str(staff_id),
                            "number": code,
                            "name": name,
                            "error_code": exc.code,
                            "reason": exc.message,
                        }
                    )
            if refused:
                raise ConflictError(
                    f"{len(refused)} of {len(ids)} could not be changed; nothing was changed.",
                    code="BULK_REFUSED",
                    details=refused,
                )

            changed: list[str] = []
            if data.action == "delete":
                changed = [labels[i][0] for i in ids]
            else:
                for staff_id in ids:
                    after = await self.staff.fetch_row(ctx, staff_id)
                    if _state(after) != states[staff_id]:
                        changed.append(labels[staff_id][0])
            if changed:
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "staff.bulk_changed",
                    "member",
                    None,
                    {
                        "action": data.action,
                        "count": len(changed),
                        "codes": changed[:_AUDIT_CODES_LIMIT],
                        "bulk": True,
                    },
                )
        return StaffBulkResult(action=data.action, affected=len(ids), changed=len(changed))

    async def _one(
        self, ctx: OrgContext, data: StaffBulkAction, staff_id: uuid.UUID, rctx: RequestContext
    ) -> None:
        match data.action:
            case "set_grade":
                await self.staff.update(
                    ctx, staff_id, StaffUpdate(grade_id=uuid.UUID(data.value or "")), rctx
                )
            case "set_approver":
                await self.staff.update(
                    ctx, staff_id, StaffUpdate(approver_id=uuid.UUID(data.value or "")), rctx
                )
            case "clear_approver":
                await self.staff.update(ctx, staff_id, StaffUpdate(approver_id=None), rctx)
            case "deactivate":
                status = "left" if data.value == "left" else "inactive"
                await self.staff.deactivate(
                    ctx,
                    staff_id,
                    StaffDeactivate(status=status, leaving_date=data.leaving_date),
                    rctx,
                )
            case "reactivate":
                await self.staff.reactivate(ctx, staff_id, rctx)
            case "delete":
                await self.staff.delete(ctx, staff_id, rctx)

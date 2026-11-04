"""The practice's branches (offices), ticket APT74.

A practice lists its branches in the settings and gives each client one (``clients.branch_id``).
With ``organizations.branches_enabled`` on, creating or editing a client needs a branch (see
``ClientsService``); off (the default), the UI hides branches and nothing is required.

* The name is unique per practice, ignoring case: 409 ``BRANCH_NAME_TAKEN``.
* A branch that clients have is deactivated, not deleted; a deactivated branch is no longer
  offered for clients, those that have it keep it.
* While branches are on, the last active branch cannot be deactivated or deleted (409
  ``LAST_ACTIVE_BRANCH``): every new client needs one. Switching branches on needs one too.
* Reading is open to every member (the client form offers the list), the client counts to
  those with ``client.read``; writing needs ``settings.manage``.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError
from app.db.models import Branch
from app.db.session import unit_of_work
from app.repositories.branches import BranchRepository
from app.repositories.organizations import OrganizationRepository
from app.schemas.branches import BranchCreate, BranchRead, BranchUpdate
from app.schemas.settings import DeleteOutcome
from app.services import policy
from app.services.common import apply_changes, duplicate_error, record_audit
from app.services.policy import OrgContext


class BranchService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.branches = BranchRepository(session)

    async def _reads(self, ctx: OrgContext, branches: list[Branch]) -> list[BranchRead]:
        counts = await self.branches.client_counts(ctx.organization_id, [b.id for b in branches])
        show = policy.has_permission(ctx, "client.read")
        return [
            BranchRead.from_model(b, client_count=counts.get(b.id, 0), show_count=show)
            for b in branches
        ]

    async def _read(self, ctx: OrgContext, branch: Branch) -> BranchRead:
        [read] = await self._reads(ctx, [branch])
        return read

    # --- Reads ----------------------------------------------------------------------------

    async def list_branches(
        self, ctx: OrgContext, *, is_active: bool | None = None, q: str | None = None
    ) -> list[BranchRead]:
        rows = await self.branches.list_all(ctx.organization_id, is_active=is_active, q=q)
        return await self._reads(ctx, rows)

    async def get(self, ctx: OrgContext, branch_id: uuid.UUID) -> BranchRead:
        branch = await self.branches.get(ctx.organization_id, branch_id)
        if branch is None:
            raise NotFoundError("Branch not found.")
        return await self._read(ctx, branch)

    # --- Writes ---------------------------------------------------------------------------

    async def create(self, ctx: OrgContext, data: BranchCreate, rctx: RequestContext) -> BranchRead:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            await self._ensure_unique(org_id, data.name, exclude_id=None)
            branch = await self.branches.add(Branch(organization_id=org_id, **data.model_dump()))
            await record_audit(
                self.session, ctx, rctx, "branch.created", "branch", branch.id,
                {"name": branch.name},
            )  # fmt: skip
            result = await self._read(ctx, branch)
        return result

    async def update(
        self, ctx: OrgContext, branch_id: uuid.UUID, data: BranchUpdate, rctx: RequestContext
    ) -> BranchRead:
        org_id = ctx.organization_id
        values = data.model_dump(exclude_unset=True)
        async with unit_of_work(self.session):
            branch = await self.branches.get(org_id, branch_id, lock=True)
            if branch is None:
                raise NotFoundError("Branch not found.")
            if "name" in values:
                await self._ensure_unique(org_id, values["name"], exclude_id=branch.id)
            if values.get("is_active") is False:
                await self._ensure_not_last_active(org_id, branch)
            changes = apply_changes(branch, values)
            branch = await self.branches.save(branch)
            if changes:
                await record_audit(
                    self.session, ctx, rctx, "branch.updated", "branch", branch.id,
                    {"name": branch.name, "changes": changes},
                )  # fmt: skip
            result = await self._read(ctx, branch)
        return result

    async def set_active(
        self, ctx: OrgContext, branch_id: uuid.UUID, active: bool, rctx: RequestContext
    ) -> BranchRead:
        return await self.update(ctx, branch_id, BranchUpdate(is_active=active), rctx)

    async def delete(
        self, ctx: OrgContext, branch_id: uuid.UUID, rctx: RequestContext
    ) -> DeleteOutcome:
        """Delete a branch no client has; deactivate one that clients (deleted ones included)
        still have."""
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            branch = await self.branches.get(org_id, branch_id, lock=True)
            if branch is None:
                raise NotFoundError("Branch not found.")
            await self._ensure_not_last_active(org_id, branch)
            meta = {"name": branch.name}
            if await self.branches.any_client(org_id, branch.id):
                if branch.is_active:
                    branch.is_active = False
                    await self.branches.save(branch)
                    await record_audit(
                        self.session, ctx, rctx, "branch.deactivated", "branch", branch.id, meta
                    )
                return DeleteOutcome(outcome="deactivated", id=branch.id)
            await self.branches.delete(org_id, branch.id)
            await record_audit(self.session, ctx, rctx, "branch.deleted", "branch", branch.id, meta)
        return DeleteOutcome(outcome="deleted", id=branch_id)

    # --- Internals ------------------------------------------------------------------------

    async def _ensure_not_last_active(self, org_id: uuid.UUID, branch: Branch) -> None:
        """409 ``LAST_ACTIVE_BRANCH`` for taking away the practice's last active branch while it
        uses branches. The organization row lock serialises this with the switch."""
        if not branch.is_active:
            return
        organization = await OrganizationRepository(self.session).lock(org_id)
        if organization is None or not organization.branches_enabled:
            return
        if not await self.branches.any_active(org_id, exclude_id=branch.id):
            raise ConflictError(
                "This is the last active branch, and every new client needs one. Add another "
                "branch, or switch branches off, first.",
                code="LAST_ACTIVE_BRANCH",
            )

    async def _ensure_unique(
        self, org_id: uuid.UUID, name: str, *, exclude_id: uuid.UUID | None
    ) -> None:
        if await self.branches.name_taken(org_id, name, exclude_id=exclude_id):
            raise duplicate_error(
                "name", "A branch with this name already exists.", "BRANCH_NAME_TAKEN"
            )

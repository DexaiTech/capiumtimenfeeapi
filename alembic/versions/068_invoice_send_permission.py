"""invoice send permission

Revision ID: 068
Revises: 067

Ticket APT55: sending an invoice to the client is its own permission, ``invoice.send``, so a
practice can let some people send invoices without letting them issue or void them (and the
other way round). ``invoice.issue`` used to cover sending too, so nothing changes for anyone:
every role that holds ``invoice.issue`` gets ``invoice.send``, and every person given or denied
``invoice.issue`` of their own is given or denied ``invoice.send`` the same way.

Downgrade deletes the permission (its role grants and personal overrides go with it) and gives
``invoice.issue`` its old description back.
"""

from collections.abc import Sequence

from alembic import op

from app.db.migration_helpers import sync_permissions

revision: str = "068"
down_revision: str | None = "067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sync_permissions()
    op.execute(
        """
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT rp.role_id, s.id FROM role_permissions rp
        JOIN permissions i ON i.id = rp.permission_id AND i.code = 'invoice.issue'
        JOIN permissions s ON s.code = 'invoice.send'
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO member_permission_overrides
            (id, organization_id, user_id, permission_code, effect, created_by)
        SELECT gen_random_uuid(), organization_id, user_id, 'invoice.send', effect, created_by
        FROM member_permission_overrides WHERE permission_code = 'invoice.issue'
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM permissions WHERE code = 'invoice.send'")
    op.execute(
        "UPDATE permissions SET description = 'Issue, send and void invoices' "
        "WHERE code = 'invoice.issue'"
    )

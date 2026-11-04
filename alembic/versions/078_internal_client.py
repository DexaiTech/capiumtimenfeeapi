"""internal client

Revision ID: 078
Revises: 077

Ticket APT64: every time entry needs a client and a job, so internal time (admin, training,
business development, holiday) goes on a built-in Internal client. Adds ``clients.is_internal``
(at most one per practice) and gives every practice its Internal client, with one non-billable
job per active non-chargeable activity code it has (job number ``INT-<code>``, named after the
code). Running the data step again adds nothing: a practice that has an Internal client keeps it,
and one that has jobs gets no more.

The Internal client takes the code ``INTERNAL``; a practice that already has a live client with
that code gets ``INTERNAL-`` and eight random characters instead. A job number already taken gets
``-`` and six random characters after it.

Downgrade removes an Internal client and its jobs only when nothing refers to them (no time, no
timer, ...). One that is still referred to is left in place as an ordinary client: the time on
it is kept. Upgrading again then gives that practice a new Internal client beside it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "078"
down_revision: str | None = "077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("is_internal", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "uq_clients_organization_id_internal",
        "clients",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("is_internal"),
    )

    op.execute(
        """
        INSERT INTO clients (id, organization_id, code, name, client_type, status, is_internal)
        SELECT gen_random_uuid(), o.id,
               CASE WHEN EXISTS (
                   SELECT 1 FROM clients c
                   WHERE c.organization_id = o.id AND c.code = 'INTERNAL'
                     AND c.deleted_at IS NULL
               )
               THEN 'INTERNAL-' || upper(substr(md5(random()::text), 1, 8))
               ELSE 'INTERNAL' END,
               'Internal', 'other', 'active', true
        FROM organizations o
        WHERE NOT EXISTS (
            SELECT 1 FROM clients c WHERE c.organization_id = o.id AND c.is_internal
        )
        """
    )
    op.execute(
        """
        INSERT INTO jobs (
            id, organization_id, client_id, number, name, status, billing_basis
        )
        SELECT gen_random_uuid(), a.organization_id, c.id,
               CASE WHEN EXISTS (
                   SELECT 1 FROM jobs j
                   WHERE j.organization_id = a.organization_id AND j.number = 'INT-' || a.code
               )
               THEN 'INT-' || a.code || '-' || upper(substr(md5(random()::text), 1, 6))
               ELSE 'INT-' || a.code END,
               a.name, 'in_progress', 'non_billable'
        FROM activity_codes a
        JOIN clients c ON c.organization_id = a.organization_id AND c.is_internal
        WHERE NOT a.is_chargeable AND a.is_active
          AND NOT EXISTS (
              SELECT 1 FROM jobs j WHERE j.organization_id = c.organization_id AND j.client_id = c.id
          )
        """
    )


def downgrade() -> None:
    # One Internal client at a time: one that something still refers to (a foreign key
    # refuses the delete) is left as it is, and the others are removed.
    op.execute(
        """
        DO $$
        DECLARE
            internal_client record;
        BEGIN
            FOR internal_client IN SELECT id, organization_id FROM clients WHERE is_internal LOOP
                BEGIN
                    DELETE FROM jobs
                    WHERE organization_id = internal_client.organization_id
                      AND client_id = internal_client.id;
                    DELETE FROM clients WHERE id = internal_client.id;
                EXCEPTION WHEN foreign_key_violation THEN
                    NULL;
                END;
            END LOOP;
        END $$;
        """
    )
    op.drop_index("uq_clients_organization_id_internal", table_name="clients")
    op.drop_column("clients", "is_internal")

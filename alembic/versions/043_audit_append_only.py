"""audit log: the append-only guard and the index behind a record's history

``audit_logs`` is append-only. A row trigger, cloned onto every monthly partition, the default
partition and every partition created later, refuses every UPDATE and DELETE. Nothing in the
application deletes audit rows and a practice is deleted softly, so there is no cascade to let
through (the table has no foreign key). TRUNCATE and dropping a partition are administrative
operations and stay possible: the test suite empties the table between tests.

The index serves ``GET /audit-logs/history`` and the record filters: the entries about one record.

Revision ID: 043
Revises: 042
"""

from collections.abc import Sequence

from alembic import op

revision: str = "043"
down_revision: str | None = "042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_logs_organization_id_target_created_at",
        "audit_logs",
        ["organization_id", "target_type", "target_id", "created_at"],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION reject_audit_log_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_audit_logs_append_only BEFORE UPDATE OR DELETE "
        "ON audit_logs FOR EACH ROW EXECUTE FUNCTION reject_audit_log_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_audit_logs_append_only ON audit_logs")
    op.execute("DROP FUNCTION reject_audit_log_change()")
    op.drop_index("ix_audit_logs_organization_id_target_created_at", table_name="audit_logs")

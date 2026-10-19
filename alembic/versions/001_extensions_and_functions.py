"""extensions and functions

Revision ID: 001
Revises:
"""

from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END
        $$
        """
    )
    # SECURITY DEFINER: the worker's role does not own the partitioned tables.
    op.execute(
        """
        CREATE FUNCTION create_monthly_partition(parent text, month date) RETURNS text
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        DECLARE
            start_date date := date_trunc('month', month)::date;
            end_date date := (date_trunc('month', month) + interval '1 month')::date;
            partition_name text := parent || '_' || to_char(start_date, 'YYYY_MM');
        BEGIN
            IF parent NOT IN ('audit_logs') THEN
                RAISE EXCEPTION 'partitions are not managed for table %', parent;
            END IF;
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                partition_name, parent, start_date, end_date
            );
            RETURN partition_name;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION create_monthly_partition(text, date)")
    op.execute("DROP FUNCTION set_updated_at()")

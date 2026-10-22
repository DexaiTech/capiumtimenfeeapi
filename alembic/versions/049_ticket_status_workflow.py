"""ticket status workflow and the ticket timeline index

Revision ID: 049
Revises: 048

The statuses of a ticket become: ``open``, ``in_progress``, ``requirement_pending``,
``clarification_required``, ``blocked``, ``ready_for_ba_review``, ``reopened``, ``closed``,
``invalid`` and ``on_hold``. ``hold`` is renamed ``on_hold`` and ``reopen`` ``reopened``, in the
tickets and in the status history. ``closed_at`` stays tied to ``closed`` alone.

Also adds the partial index that finds the audit entries naming one ticket (``ticket_id``: its
comments and files), which the ticket timeline reads next to the entries about the ticket itself.

The way down is lossy: ``on_hold`` and ``reopened`` go back to their old names, ``invalid`` to
``closed`` (``closed_at`` is filled from ``updated_at``) and the four waiting statuses to
``in_progress``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "049"
down_revision: str | None = "048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("open", "hold", "in_progress", "reopen", "closed")
NEW = (
    "open",
    "in_progress",
    "requirement_pending",
    "clarification_required",
    "blocked",
    "ready_for_ba_review",
    "reopened",
    "closed",
    "invalid",
    "on_hold",
)
RENAMED = {"hold": "on_hold", "reopen": "reopened"}
WAITING = ("requirement_pending", "clarification_required", "blocked", "ready_for_ba_review")


def _list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _rename(mapping: dict[str, str]) -> None:
    for old, new in mapping.items():
        op.execute(
            sa.text("UPDATE tickets SET status = :new WHERE status = :old").bindparams(
                old=old, new=new
            )
        )
        for column in ("from_status", "to_status"):
            op.execute(
                sa.text(
                    f"UPDATE ticket_status_history SET {column} = :new WHERE {column} = :old"
                ).bindparams(old=old, new=new)
            )


def upgrade() -> None:
    # The old checks go first: the renamed values are not in them.
    op.drop_constraint(op.f("ck_tickets_status"), "tickets", type_="check")
    op.drop_constraint(
        op.f("ck_ticket_status_history_from_status"), "ticket_status_history", "check"
    )
    op.drop_constraint(op.f("ck_ticket_status_history_to_status"), "ticket_status_history", "check")
    _rename(RENAMED)
    op.create_check_constraint(op.f("ck_tickets_status"), "tickets", f"status IN ({_list(NEW)})")
    op.create_check_constraint(
        op.f("ck_ticket_status_history_from_status"),
        "ticket_status_history",
        f"from_status IS NULL OR from_status IN ({_list(NEW)})",
    )
    op.create_check_constraint(
        op.f("ck_ticket_status_history_to_status"),
        "ticket_status_history",
        f"to_status IN ({_list(NEW)})",
    )
    op.create_index(
        "ix_audit_logs_organization_id_ticket_ref_created_at",
        "audit_logs",
        ["organization_id", sa.literal_column("(metadata ->> 'ticket_id')"), "created_at"],
        unique=False,
        postgresql_where=sa.text("(metadata ->> 'ticket_id') IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_organization_id_ticket_ref_created_at", table_name="audit_logs")
    op.drop_constraint(op.f("ck_tickets_status"), "tickets", type_="check")
    op.drop_constraint(
        op.f("ck_ticket_status_history_from_status"), "ticket_status_history", "check"
    )
    op.drop_constraint(op.f("ck_ticket_status_history_to_status"), "ticket_status_history", "check")
    # One statement: ``closed_at`` must be set exactly while the status is ``closed``.
    op.execute(
        "UPDATE tickets SET status = 'closed', closed_at = coalesce(closed_at, updated_at) "
        "WHERE status = 'invalid'"
    )
    op.execute(f"UPDATE tickets SET status = 'in_progress' WHERE status IN ({_list(WAITING)})")
    for column in ("from_status", "to_status"):
        op.execute(
            f"UPDATE ticket_status_history SET {column} = 'closed' WHERE {column} = 'invalid'"
        )
        op.execute(
            f"UPDATE ticket_status_history SET {column} = 'in_progress' "
            f"WHERE {column} IN ({_list(WAITING)})"
        )
    _rename({new: old for old, new in RENAMED.items()})
    # A history row whose two ends became equal would break ``status_changed``: drop those.
    op.execute("DELETE FROM ticket_status_history WHERE from_status = to_status")
    op.create_check_constraint(op.f("ck_tickets_status"), "tickets", f"status IN ({_list(OLD)})")
    op.create_check_constraint(
        op.f("ck_ticket_status_history_from_status"),
        "ticket_status_history",
        f"from_status IS NULL OR from_status IN ({_list(OLD)})",
    )
    op.create_check_constraint(
        op.f("ck_ticket_status_history_to_status"),
        "ticket_status_history",
        f"to_status IN ({_list(OLD)})",
    )

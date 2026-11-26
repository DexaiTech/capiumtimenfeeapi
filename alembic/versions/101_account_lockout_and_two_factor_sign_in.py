"""account lockout and two-factor sign-in

Revision ID: 101
Revises: 100

Ticket ONELOGIN (docs/implementation-plans/ONELOGIN.md): the Capium OneLogin sign-in rules in
Time & Fees' own authentication.

* ``users``: ``failed_login_count`` (consecutive wrong passwords or codes, 0 for everyone) and
  ``locked_until`` (null: not locked); ``mfa_secret`` / ``mfa_pending_secret`` (encrypted TOTP
  secrets, null: no two-factor), ``mfa_enabled_at`` (set together with ``mfa_secret``,
  ``ck_users_mfa_secret_matches_enabled``) and ``mfa_last_step`` (the last code accepted).
  Existing accounts start unlocked and without two-factor; nothing about how they sign in changes.
* ``mfa_challenges``: two-factor sign-ins in progress (expiry, attempts up to ``max_attempts``,
  used or revoked), removed with their user.

Downgrade drops the table and the columns: open challenges are lost and everyone who turned
two-factor on signs in with the password alone again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "101"
down_revision: str | None = "100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ID = mysql.CHAR(36, charset="ascii", collation="ascii_bin")


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("failed_login_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("users", sa.Column("locked_until", mysql.DATETIME(fsp=6), nullable=True))
    op.add_column("users", sa.Column("mfa_secret", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("mfa_pending_secret", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("mfa_enabled_at", mysql.DATETIME(fsp=6), nullable=True))
    op.add_column("users", sa.Column("mfa_last_step", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_users_failed_login_count"), "users", "failed_login_count >= 0"
    )
    op.create_check_constraint(
        op.f("ck_users_mfa_secret_matches_enabled"),
        "users",
        "(mfa_secret IS NULL) = (mfa_enabled_at IS NULL)",
    )

    op.create_table(
        "mfa_challenges",
        sa.Column("user_id", _ID, nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("used_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("revoked_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("id", _ID, nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND max_attempts",
            name=op.f("ck_mfa_challenges_attempt_count"),
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 20", name=op.f("ck_mfa_challenges_max_attempts")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_mfa_challenges_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mfa_challenges")),
    )
    op.create_index("ix_mfa_challenges_user_id", "mfa_challenges", ["user_id"])
    op.create_index("uq_mfa_challenges_token_hash", "mfa_challenges", ["token_hash"], unique=True)


def downgrade() -> None:
    # The table goes with its indexes (MySQL refuses to drop the user_id index first: the
    # foreign key needs it).
    op.drop_table("mfa_challenges")
    op.drop_constraint(op.f("ck_users_mfa_secret_matches_enabled"), "users", type_="check")
    op.drop_constraint(op.f("ck_users_failed_login_count"), "users", type_="check")
    for column in (
        "mfa_last_step",
        "mfa_enabled_at",
        "mfa_pending_secret",
        "mfa_secret",
        "locked_until",
        "failed_login_count",
    ):
        op.drop_column("users", column)

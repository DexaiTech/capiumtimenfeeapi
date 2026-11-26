import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, SoftDeleteMixin, TimestampMixin
from app.db.types import NOW, UTCDateTime, Uuid36


class User(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("char_length(full_name) BETWEEN 1 AND 120", name="full_name_length"),
        CheckConstraint(
            "(avatar_storage_key IS NULL) = (avatar_content_type IS NULL) "
            "AND (avatar_storage_key IS NULL) = (avatar_updated_at IS NULL)",
            name="avatar_all_or_none",
        ),
        Index(
            "uq_users_email_lower",
            text("(CASE WHEN deleted_at IS NULL THEN lower(email) END)"),
            unique=True,
        ),
        CheckConstraint("failed_login_count >= 0", name="failed_login_count"),
        CheckConstraint(
            "(mfa_secret IS NULL) = (mfa_enabled_at IS NULL)", name="mfa_secret_matches_enabled"
        ),
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default="Europe/London"
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Set for accounts whose password an administrator chose; cleared when the person
    # changes it. While set, the API answers PASSWORD_CHANGE_REQUIRED (see dependencies/auth).
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    password_changed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=NOW
    )
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Account lockout (the OneLogin rule): consecutive wrong passwords or codes since the last
    # successful sign-in, and until when every sign-in is refused (docs/modules/foundation.md).
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Two-factor sign-in (TOTP). The secrets are encrypted (app/core/totp.py) and never returned
    # after set-up; ``mfa_pending_secret`` waits for the first code; ``mfa_last_step`` is the
    # 30-second step of the last code accepted, so a code is never accepted twice.
    mfa_secret: Mapped[str | None] = mapped_column(String(255))
    mfa_pending_secret: Mapped[str | None] = mapped_column(String(255))
    mfa_enabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    mfa_last_step: Mapped[int | None] = mapped_column(BigInteger)
    # The profile picture: a key in the file store (namespaced by the user's id), its content type
    # and when it was set (also the cache-buster of the image). All three are set or all are null.
    avatar_storage_key: Mapped[str | None] = mapped_column(Text)
    avatar_content_type: Mapped[str | None] = mapped_column(Text)
    avatar_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def two_factor_enabled(self) -> bool:
        return self.mfa_enabled_at is not None

    def locked_at(self, now: datetime) -> bool:
        return self.locked_until is not None and self.locked_until > now


class UserSession(Base, IdMixin, TimestampMixin):
    __tablename__ = "user_sessions"
    __table_args__ = (
        CheckConstraint(
            "revoked_reason IS NULL OR revoked_reason IN "
            "('logout', 'password_change', 'reuse_detected', 'admin', 'user')",
            name="revoked_reason",
        ),
        Index("ix_user_sessions_user_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid36(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=NOW
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_reason: Mapped[str | None] = mapped_column(String(32))


class RefreshToken(Base, IdMixin, TimestampMixin):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("uq_refresh_tokens_token_hash", "token_hash", unique=True),
        Index("ix_refresh_tokens_session_id", "session_id"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid36(), ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid36(), ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )


class _OneTimeTokenMixin:
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    requested_ip: Mapped[str | None] = mapped_column(String(45))


class EmailVerificationToken(Base, IdMixin, TimestampMixin, _OneTimeTokenMixin):
    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        Index("uq_email_verification_tokens_token_hash", "token_hash", unique=True),
        Index("ix_email_verification_tokens_user_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid36(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


class PasswordResetToken(Base, IdMixin, TimestampMixin, _OneTimeTokenMixin):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        Index("uq_password_reset_tokens_token_hash", "token_hash", unique=True),
        Index("ix_password_reset_tokens_user_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid36(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


class MfaChallenge(Base, IdMixin, TimestampMixin):
    """A two-factor sign-in in progress (OneLogin's ``MfaToken``): created when the password was
    right for an account with two-factor on, answered with one code. It expires, counts wrong
    codes up to ``max_attempts`` (then it is revoked) and can be used once."""

    __tablename__ = "mfa_challenges"
    __table_args__ = (
        Index("uq_mfa_challenges_token_hash", "token_hash", unique=True),
        Index("ix_mfa_challenges_user_id", "user_id"),
        CheckConstraint("max_attempts BETWEEN 1 AND 20", name="max_attempts"),
        CheckConstraint("attempt_count BETWEEN 0 AND max_attempts", name="attempt_count"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid36(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(Text)

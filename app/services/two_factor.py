"""Two-factor sign-in of the signed-in person: set it up, confirm it with a first code, turn it off.

Set-up stores a new secret as pending and returns it once (secret and ``otpauth://`` URI); it
becomes active only when a code from it is confirmed, so a half-finished set-up never locks
anyone out. Turning it on or off ends the person's other sessions and any two-factor sign-in in
progress, as a password change does, and is audited. Codes here do not count toward the account
lockout: the caller is already signed in.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import RequestContext
from app.core.exceptions import AuthenticationError, ConflictError, ValidationError
from app.core.security import verify_password
from app.core.totp import (
    SecretUnreadableError,
    decrypt_secret,
    encrypt_secret,
    matching_step,
    new_secret,
    provisioning_uri,
)
from app.db.models import User
from app.db.session import unit_of_work
from app.repositories.mfa import MfaChallengeRepository
from app.schemas.auth import TwoFactorSetup
from app.services import audit
from app.services.sessions import SessionService


def _code_invalid() -> ValidationError:
    return ValidationError(
        "The code is incorrect. Check that your phone's clock is right and try again.",
        code="MFA_CODE_INVALID",
        details=[{"field": "code", "code": "invalid", "message": "The code is incorrect."}],
    )


def _already_on() -> ConflictError:
    return ConflictError("Two-factor sign-in is already on.", code="MFA_ALREADY_ENABLED")


class TwoFactorService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.sessions = SessionService(session, settings)
        self.challenges = MfaChallengeRepository(session)

    async def setup(self, user: User) -> TwoFactorSetup:
        if user.two_factor_enabled:
            raise _already_on()
        secret = new_secret()
        async with unit_of_work(self.session):
            user = await self.session.merge(user)
            user.mfa_pending_secret = encrypt_secret(self.settings, secret)
        return TwoFactorSetup(
            secret=secret,
            otpauth_uri=provisioning_uri(secret, user.email, self.settings.app_name),
        )

    async def enable(
        self, user: User, code: str, current_session_id: uuid.UUID, ctx: RequestContext
    ) -> User:
        async with unit_of_work(self.session):
            user = await self.session.merge(user)
            await self.session.refresh(user, with_for_update=True)
            if user.two_factor_enabled:
                raise _already_on()
            if user.mfa_pending_secret is None:
                raise ConflictError(
                    "Start the set-up first: scan the code, then enter a code from the app.",
                    code="MFA_SETUP_REQUIRED",
                )
            now = datetime.now(UTC)
            step = matching_step(self._read(user.mfa_pending_secret), code, now=now, last_step=None)
            if step is None:
                raise _code_invalid()
            user.mfa_secret = user.mfa_pending_secret
            user.mfa_pending_secret = None
            user.mfa_enabled_at = now
            user.mfa_last_step = step
            await self._end_other_sign_ins(user, current_session_id, now)
            await audit.record(
                self.session,
                "auth.mfa_enabled",
                ctx=ctx,
                actor_user_id=user.id,
                target_type="user",
                target_id=user.id,
            )
        return user

    async def disable(
        self,
        user: User,
        password: str,
        code: str,
        current_session_id: uuid.UUID,
        ctx: RequestContext,
    ) -> User:
        valid, _ = await verify_password(password, user.password_hash)
        if not valid:
            # 400, not 401: the caller is signed in and must not be sent to refresh.
            raise AuthenticationError(
                "Your password is incorrect.", code="AUTH_INVALID_CREDENTIALS", status_code=400
            )
        async with unit_of_work(self.session):
            user = await self.session.merge(user)
            await self.session.refresh(user, with_for_update=True)
            if user.mfa_secret is None:
                raise ConflictError("Two-factor sign-in is not on.", code="MFA_NOT_ENABLED")
            now = datetime.now(UTC)
            step = matching_step(
                self._read(user.mfa_secret), code, now=now, last_step=user.mfa_last_step
            )
            if step is None:
                raise _code_invalid()
            clear_two_factor(user)
            await self._end_other_sign_ins(user, current_session_id, now)
            await audit.record(
                self.session,
                "auth.mfa_disabled",
                ctx=ctx,
                actor_user_id=user.id,
                target_type="user",
                target_id=user.id,
            )
        return user

    async def _end_other_sign_ins(
        self, user: User, current_session_id: uuid.UUID, now: datetime
    ) -> None:
        await self.challenges.revoke_open(user.id, now)
        await self.sessions.revoke_all(user.id, "user", except_id=current_session_id)

    def _read(self, stored: str) -> str:
        try:
            return decrypt_secret(self.settings, stored)
        except SecretUnreadableError as exc:
            raise ConflictError(
                "Two-factor sign-in cannot be read. Ask an administrator to reset it.",
                code="MFA_SECRET_UNREADABLE",
            ) from exc


def clear_two_factor(user: User) -> None:
    user.mfa_secret = None
    user.mfa_pending_secret = None
    user.mfa_enabled_at = None
    user.mfa_last_step = None

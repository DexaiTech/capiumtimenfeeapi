"""Registration, login, token refresh, logout, email verification and password reset.

Sign-in follows the Capium OneLogin rules (docs/modules/foundation.md, "Sign-in rules"):

* **Lockout.** Each wrong password or two-factor code for an existing account counts; at
  ``LOGIN_LOCKOUT_THRESHOLD`` consecutive failures the account refuses every sign-in (423
  ``AUTH_ACCOUNT_LOCKED``) for ``LOGIN_LOCKOUT_MINUTES``. A successful sign-in, a password reset
  and an administrator's unlock clear it. An unknown email counts nothing and answers the same 401.
* **Two-factor.** For an account with two-factor on, a right password starts no session: it
  opens a challenge (``mfa_challenges``) answered by ``validate_two_factor`` with a code. A
  challenge expires, takes at most ``max_attempts`` wrong codes (then it is revoked) and is used
  once. A code is checked under the account's row lock, so it is accepted at most once.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import RequestContext
from app.core.exceptions import (
    AppException,
    AuthenticationError,
    AuthorizationError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import generate_token, hash_password, hash_token, verify_password
from app.core.tokens import create_access_token
from app.core.totp import SecretUnreadableError, decrypt_secret, matching_step
from app.db.models import (
    EmailVerificationToken,
    MfaChallenge,
    PasswordResetToken,
    User,
    UserSession,
)
from app.db.session import unit_of_work
from app.repositories.auth_tokens import AuthTokenRepository
from app.repositories.mfa import MfaChallengeRepository
from app.repositories.users import UserRepository
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TwoFactorValidateRequest,
)
from app.services import audit, jobs
from app.services.rate_limit import RateLimiter, ScaledRateLimiter
from app.services.sessions import RotationStatus, SessionService
from app.utils.ids import new_id

log = get_logger(__name__)

VERIFICATION_TTL = timedelta(hours=24)
RESET_TTL = timedelta(hours=1)
HOUR, QUARTER_HOUR = 3600, 900


@dataclass(slots=True)
class TokenBundle:
    access_token: str
    expires_in: int
    refresh_token: str
    user: User
    session: UserSession


@dataclass(slots=True)
class TwoFactorRequired:
    """The password was right; the sign-in waits for a code (``validate_two_factor``)."""

    challenge_token: str
    expires_in: int


def locked_error(user: User, now: datetime) -> AppException:
    assert user.locked_until is not None
    seconds = max(1, math.ceil((user.locked_until - now).total_seconds()))
    minutes = math.ceil(seconds / 60)
    return AppException(
        f"Too many failed sign-ins. Try again in {minutes} minute{'s' if minutes != 1 else ''} "
        "or reset your password.",
        code="AUTH_ACCOUNT_LOCKED",
        status_code=423,
        details={"retry_after": seconds},
        headers={"Retry-After": str(seconds)},
    )


def _invalid_credentials() -> AuthenticationError:
    return AuthenticationError(
        "The email or password is incorrect.", code="AUTH_INVALID_CREDENTIALS"
    )


def _code_invalid(attempts_left: int | None = None) -> AuthenticationError:
    return AuthenticationError(
        "The code is incorrect.",
        code="AUTH_MFA_CODE_INVALID",
        details={"attempts_left": attempts_left} if attempts_left is not None else None,
    )


def _challenge_invalid() -> AuthenticationError:
    return AuthenticationError(
        "This sign-in has expired. Enter your email and password again.",
        code="AUTH_MFA_CHALLENGE_INVALID",
    )


def _refresh_invalid() -> AuthenticationError:
    return AuthenticationError(
        "Your session has ended. Sign in again.", code="AUTH_REFRESH_INVALID"
    )


def _token_invalid() -> ValidationError:
    return ValidationError("This link is invalid or has expired.", code="TOKEN_INVALID")


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings, limiter: RateLimiter) -> None:
        self.session = session
        self.settings = settings
        self.limiter = ScaledRateLimiter(limiter, settings.auth_rate_limit_factor)
        self.users = UserRepository(session)
        self.tokens = AuthTokenRepository(session)
        self.sessions = SessionService(session, settings)
        self.challenges = MfaChallengeRepository(session)

    # -- registration and verification -------------------------------------------------

    async def register(self, data: RegisterRequest, ctx: RequestContext) -> None:
        """Create an unverified account. Behaves identically for an already-registered email."""
        await self.limiter.hit(f"register:ip:{ctx.ip_address}", limit=5, window_seconds=HOUR)
        # Hash before looking the email up so both branches take the same time.
        password_hash = await hash_password(data.password)
        try:
            async with unit_of_work(self.session):
                existing = await self.users.get_by_email(data.email)
                if existing is not None:
                    await jobs.enqueue(
                        self.session,
                        "send_email",
                        {
                            "template": "account_exists",
                            "to": existing.email,
                            "name": existing.full_name,
                        },
                        dedupe_key=f"account_exists:{existing.id}",
                    )
                    return
                user = await self.users.add(
                    User(
                        id=new_id(),
                        email=data.email,
                        password_hash=password_hash,
                        full_name=data.full_name,
                    )
                )
                await self._send_verification(user, ctx)
                await audit.record(
                    self.session,
                    "auth.registered",
                    ctx=ctx,
                    actor_user_id=user.id,
                    target_type="user",
                    target_id=user.id,
                )
        except IntegrityError:
            # Lost a race with a concurrent registration of the same email: same response.
            log.info("register_duplicate_race")

    async def _send_verification(self, user: User, ctx: RequestContext) -> None:
        now = datetime.now(UTC)
        await self.tokens.invalidate_for_user(EmailVerificationToken, user.id, now)
        raw = generate_token()
        await self.tokens.add(
            EmailVerificationToken(
                id=new_id(),
                user_id=user.id,
                token_hash=hash_token(raw),
                expires_at=now + VERIFICATION_TTL,
                requested_ip=ctx.ip_address,
            )
        )
        await jobs.enqueue(
            self.session,
            "send_email",
            {"template": "verification", "to": user.email, "name": user.full_name, "token": raw},
        )

    async def resend_verification(self, email: str, ctx: RequestContext) -> None:
        await self.limiter.hit(f"resend:ip:{ctx.ip_address}", limit=10, window_seconds=HOUR)
        await self.limiter.hit(f"resend:email:{email.lower()}", limit=3, window_seconds=HOUR)
        async with unit_of_work(self.session):
            user = await self.users.get_by_email(email)
            if user is not None and user.is_active and not user.email_verified:
                await self._send_verification(user, ctx)

    async def verify_email(self, raw_token: str, ctx: RequestContext) -> None:
        await self.limiter.hit(f"verify:ip:{ctx.ip_address}", limit=10, window_seconds=HOUR)
        async with unit_of_work(self.session):
            token = await self.tokens.get_usable(EmailVerificationToken, hash_token(raw_token))
            user = await self.users.get(token.user_id) if token else None
            if token is None or user is None:
                raise _token_invalid()
            now = datetime.now(UTC)
            token.used_at = now
            if not user.email_verified:
                user.email_verified_at = now
                await jobs.enqueue(
                    self.session,
                    "send_email",
                    {"template": "welcome", "to": user.email, "name": user.full_name},
                )
            await audit.record(
                self.session,
                "auth.email_verified",
                ctx=ctx,
                actor_user_id=user.id,
                target_type="user",
                target_id=user.id,
            )

    # -- login, refresh, logout --------------------------------------------------------

    def _bundle(self, user: User, user_session: UserSession, refresh_token: str) -> TokenBundle:
        access_token, expires_in = create_access_token(
            self.settings, user_id=user.id, session_id=user_session.id
        )
        return TokenBundle(access_token, expires_in, refresh_token, user, user_session)

    async def login(
        self, data: LoginRequest, ctx: RequestContext
    ) -> TokenBundle | TwoFactorRequired:
        email = data.email.lower()
        await self.limiter.hit(f"login:ip:{ctx.ip_address}", limit=10, window_seconds=QUARTER_HOUR)
        user = await self.users.get_by_email(email)
        now = datetime.now(UTC)
        # A locked account answers 423 before the per-email limit: otherwise that limit (429,
        # counting the very failures that locked it) would hide the lock and its retry time.
        # Answering without checking the password tells nothing the 423 does not already say.
        if user is not None and user.locked_at(now):
            async with unit_of_work(self.session):
                await audit.record(
                    self.session, "auth.login_locked", ctx=ctx, actor_user_id=user.id
                )
            raise locked_error(user, now)
        await self.limiter.hit(f"login:email:{email}", limit=5, window_seconds=QUARTER_HOUR)

        # Always verified, so an unknown account takes as long as a known one.
        valid, upgraded_hash = await verify_password(
            data.password, user.password_hash if user else None
        )
        if user is None or not valid:
            error: AppException = _invalid_credentials()
            async with unit_of_work(self.session):
                if user is not None:
                    error = await self._count_failure(user, ctx) or error
                await audit.record(
                    self.session,
                    "auth.login_failed",
                    ctx=ctx,
                    actor_user_id=user.id if user else None,
                )
            log.warning("login_failed")
            raise error
        if not user.is_active:
            raise AuthorizationError("This account is disabled.", code="AUTH_ACCOUNT_DISABLED")

        if not user.two_factor_enabled:
            async with unit_of_work(self.session):
                bundle = await self._start_session(user, ctx, upgraded_hash=upgraded_hash)
            return bundle
        if data.totp_code is None:
            return await self._open_challenge(user, ctx, upgraded_hash)
        # Password and code in one request.
        failure: AppException | None = None
        async with unit_of_work(self.session):
            await self.session.refresh(user, with_for_update=True)
            step = self._code_step(user, data.totp_code)
            if step is None:
                failure = await self._count_failure(user, ctx) or _code_invalid()
                await audit.record(self.session, "auth.mfa_failed", ctx=ctx, actor_user_id=user.id)
            else:
                bundle = await self._start_session(
                    user, ctx, upgraded_hash=upgraded_hash, mfa_step=step
                )
        if failure is not None:
            raise failure
        return bundle

    async def validate_two_factor(
        self, data: TwoFactorValidateRequest, ctx: RequestContext
    ) -> TokenBundle:
        """The second step of a two-factor sign-in: the challenge from ``login`` and a code."""
        await self.limiter.hit(f"mfa:ip:{ctx.ip_address}", limit=20, window_seconds=QUARTER_HOUR)
        failure: AppException | None = None
        async with unit_of_work(self.session):
            now = datetime.now(UTC)
            challenge = await self.challenges.get_for_update(hash_token(data.challenge_token))
            user = await self.users.get(challenge.user_id) if challenge else None
            if user is not None:
                await self.session.refresh(user, with_for_update=True)
            if (
                challenge is None
                or challenge.used_at is not None
                or challenge.revoked_at is not None
                or challenge.expires_at <= now
            ):
                failure = _challenge_invalid()
            elif user is None or not user.is_active or not user.two_factor_enabled:
                challenge.revoked_at = now
                failure = _challenge_invalid()
            elif user.locked_at(now):
                failure = locked_error(user, now)
            else:
                step = self._code_step(user, data.code)
                if step is None:
                    challenge.attempt_count += 1
                    left = challenge.max_attempts - challenge.attempt_count
                    failure = _code_invalid(left)
                    if left <= 0:
                        challenge.revoked_at = now
                        failure = AuthenticationError(
                            "Too many wrong codes. Enter your email and password again.",
                            code="AUTH_MFA_TOO_MANY_ATTEMPTS",
                        )
                    failure = await self._count_failure(user, ctx) or failure
                    await audit.record(
                        self.session, "auth.mfa_failed", ctx=ctx, actor_user_id=user.id
                    )
                else:
                    challenge.used_at = now
                    bundle = await self._start_session(user, ctx, mfa_step=step)
        if failure is not None:
            log.warning("mfa_failed", code=failure.code)
            raise failure
        return bundle

    # -- sign-in internals -----------------------------------------------------------------

    async def _start_session(
        self,
        user: User,
        ctx: RequestContext,
        *,
        upgraded_hash: str | None = None,
        mfa_step: int | None = None,
    ) -> TokenBundle:
        """A successful sign-in, inside the caller's unit of work: clears the failure count."""
        if upgraded_hash:
            user.password_hash = upgraded_hash
        user.last_login_at = datetime.now(UTC)
        user.failed_login_count = 0
        user.locked_until = None
        if mfa_step is not None:
            user.mfa_last_step = mfa_step
        user_session, refresh_token = await self.sessions.create(user, ctx)
        await audit.record(
            self.session,
            "auth.mfa_login" if mfa_step is not None else "auth.login",
            ctx=ctx,
            actor_user_id=user.id,
            target_type="session",
            target_id=user_session.id,
        )
        return self._bundle(user, user_session, refresh_token)

    async def _open_challenge(
        self, user: User, ctx: RequestContext, upgraded_hash: str | None
    ) -> TwoFactorRequired:
        raw = generate_token()
        lifetime = timedelta(minutes=self.settings.mfa_challenge_minutes)
        async with unit_of_work(self.session):
            if upgraded_hash:
                user.password_hash = upgraded_hash
            await self.challenges.add(
                MfaChallenge(
                    user_id=user.id,
                    token_hash=hash_token(raw),
                    expires_at=datetime.now(UTC) + lifetime,
                    max_attempts=self.settings.mfa_challenge_max_attempts,
                    ip_address=ctx.ip_address,
                    user_agent=ctx.user_agent,
                )
            )
            await audit.record(
                self.session, "auth.mfa_challenge_started", ctx=ctx, actor_user_id=user.id
            )
        return TwoFactorRequired(raw, int(lifetime.total_seconds()))

    async def _count_failure(self, user: User, ctx: RequestContext) -> AppException | None:
        """One more consecutive failure, inside the caller's unit of work; locks the account at
        the threshold and returns the 423 to answer with, else None."""
        await self.session.refresh(user, with_for_update=True)
        now = datetime.now(UTC)
        user.failed_login_count += 1
        if user.failed_login_count < self.settings.login_lockout_threshold:
            return None
        # Locked: the count starts again from zero once the lock has passed.
        user.failed_login_count = 0
        user.locked_until = now + timedelta(minutes=self.settings.login_lockout_minutes)
        await audit.record(
            self.session,
            "auth.account_locked",
            ctx=ctx,
            actor_user_id=user.id,
            target_type="user",
            target_id=user.id,
        )
        log.warning("account_locked")
        return locked_error(user, now)

    def _code_step(self, user: User, code: str) -> int | None:
        if user.mfa_secret is None:
            return None
        try:
            secret = decrypt_secret(self.settings, user.mfa_secret)
        except SecretUnreadableError:
            log.error("mfa_secret_unreadable")
            return None
        return matching_step(secret, code, now=datetime.now(UTC), last_step=user.mfa_last_step)

    async def refresh(self, raw_token: str | None, ctx: RequestContext) -> TokenBundle:
        if not raw_token:
            raise _refresh_invalid()
        # The outcome is committed first: a detected reuse must revoke the session even
        # though the request itself fails.
        async with unit_of_work(self.session):
            rotation = await self.sessions.rotate(raw_token)
            if rotation.status is RotationStatus.REUSED and rotation.user and rotation.session:
                await audit.record(
                    self.session,
                    "auth.refresh_reuse_detected",
                    ctx=ctx,
                    actor_user_id=rotation.user.id,
                    target_type="session",
                    target_id=rotation.session.id,
                )
        if rotation.status is RotationStatus.REUSED:
            log.warning("refresh_reuse_detected")
        if (
            rotation.status is not RotationStatus.OK
            or not rotation.user
            or not rotation.session
            or not rotation.refresh_token
        ):
            raise _refresh_invalid()
        await self.limiter.hit(
            f"refresh:session:{rotation.session.id}", limit=60, window_seconds=HOUR
        )
        return self._bundle(rotation.user, rotation.session, rotation.refresh_token)

    async def logout(self, raw_token: str | None, ctx: RequestContext) -> None:
        if not raw_token:
            return
        async with unit_of_work(self.session):
            user_session = await self.sessions.revoke_by_token(raw_token)
            if user_session is not None:
                await audit.record(
                    self.session,
                    "auth.logout",
                    ctx=ctx,
                    actor_user_id=user_session.user_id,
                    target_type="session",
                    target_id=user_session.id,
                )

    async def logout_all(self, user: User, ctx: RequestContext) -> int:
        async with unit_of_work(self.session):
            revoked = await self.sessions.revoke_all(user.id, "user")
            await audit.record(
                self.session,
                "auth.logout_all",
                ctx=ctx,
                actor_user_id=user.id,
                meta={"revoked": revoked},
            )
        return revoked

    # -- password reset ----------------------------------------------------------------

    async def forgot_password(self, email: str, ctx: RequestContext) -> None:
        await self.limiter.hit(f"forgot:ip:{ctx.ip_address}", limit=10, window_seconds=HOUR)
        await self.limiter.hit(f"forgot:email:{email.lower()}", limit=3, window_seconds=HOUR)
        async with unit_of_work(self.session):
            user = await self.users.get_by_email(email)
            if user is None or not user.is_active:
                return
            now = datetime.now(UTC)
            await self.tokens.invalidate_for_user(PasswordResetToken, user.id, now)
            raw = generate_token()
            await self.tokens.add(
                PasswordResetToken(
                    id=new_id(),
                    user_id=user.id,
                    token_hash=hash_token(raw),
                    expires_at=now + RESET_TTL,
                    requested_ip=ctx.ip_address,
                )
            )
            await jobs.enqueue(
                self.session,
                "send_email",
                {
                    "template": "password_reset",
                    "to": user.email,
                    "name": user.full_name,
                    "token": raw,
                },
            )
            await audit.record(
                self.session, "auth.password_reset_requested", ctx=ctx, actor_user_id=user.id
            )

    async def reset_password(self, data: ResetPasswordRequest, ctx: RequestContext) -> None:
        await self.limiter.hit(f"reset:ip:{ctx.ip_address}", limit=10, window_seconds=HOUR)
        password_hash = await hash_password(data.password)
        async with unit_of_work(self.session):
            token = await self.tokens.get_usable(PasswordResetToken, hash_token(data.token))
            user = await self.users.get(token.user_id) if token else None
            if token is None or user is None:
                raise _token_invalid()
            now = datetime.now(UTC)
            token.used_at = now
            user.password_hash = password_hash
            user.password_changed_at = now
            user.must_change_password = False
            # A new password also ends a lockout (the way back for someone locked out).
            user.failed_login_count = 0
            user.locked_until = None
            await self.sessions.revoke_all(user.id, "password_change")
            await jobs.enqueue(
                self.session,
                "send_email",
                {"template": "password_changed", "to": user.email, "name": user.full_name},
            )
            await audit.record(
                self.session,
                "auth.password_reset",
                ctx=ctx,
                actor_user_id=user.id,
                target_type="user",
                target_id=user.id,
            )

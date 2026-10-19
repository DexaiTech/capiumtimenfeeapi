"""Registration, login, token refresh, logout, email verification and password reset."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.context import RequestContext
from app.core.exceptions import AuthenticationError, AuthorizationError, ValidationError
from app.core.logging import get_logger
from app.core.security import generate_token, hash_password, hash_token, verify_password
from app.core.tokens import create_access_token
from app.db.models import EmailVerificationToken, PasswordResetToken, User, UserSession
from app.db.session import unit_of_work
from app.repositories.auth_tokens import AuthTokenRepository
from app.repositories.users import UserRepository
from app.schemas.auth import LoginRequest, RegisterRequest, ResetPasswordRequest
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

    async def login(self, data: LoginRequest, ctx: RequestContext) -> TokenBundle:
        email = data.email.lower()
        await self.limiter.hit(f"login:ip:{ctx.ip_address}", limit=10, window_seconds=QUARTER_HOUR)
        await self.limiter.hit(f"login:email:{email}", limit=5, window_seconds=QUARTER_HOUR)

        user = await self.users.get_by_email(email)
        valid, upgraded_hash = await verify_password(
            data.password, user.password_hash if user else None
        )
        if user is None or not valid:
            async with unit_of_work(self.session):
                await audit.record(
                    self.session,
                    "auth.login_failed",
                    ctx=ctx,
                    actor_user_id=user.id if user else None,
                )
            log.warning("login_failed")
            raise AuthenticationError(
                "The email or password is incorrect.", code="AUTH_INVALID_CREDENTIALS"
            )
        if not user.is_active:
            raise AuthorizationError("This account is disabled.", code="AUTH_ACCOUNT_DISABLED")

        async with unit_of_work(self.session):
            if upgraded_hash:
                user.password_hash = upgraded_hash
            user.last_login_at = datetime.now(UTC)
            user_session, refresh_token = await self.sessions.create(user, ctx)
            await audit.record(
                self.session,
                "auth.login",
                ctx=ctx,
                actor_user_id=user.id,
                target_type="session",
                target_id=user_session.id,
            )
        return self._bundle(user, user_session, refresh_token)

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

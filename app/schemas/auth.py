from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.core.security import COMMON_PASSWORDS
from app.schemas.common import RequestModel
from app.schemas.user import UserRead

PASSWORD_MIN, PASSWORD_MAX = 10, 128

# A six-digit code from an authenticator app (TOTP).
TotpCode = Annotated[
    str,
    Field(
        min_length=6,
        max_length=6,
        pattern=r"^[0-9]{6}$",
        description="Six-digit code from the authenticator app",
    ),
]


class _NewPassword(RequestModel):
    # Passwords are never trimmed: surrounding spaces are part of the secret.
    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)
    password_confirm: str = Field(max_length=PASSWORD_MAX)

    @model_validator(mode="after")
    def _check_password(self) -> Self:
        if self.password.lower() in COMMON_PASSWORDS:
            raise ValueError("password: This password is too common. Choose another.")
        if self.password != self.password_confirm:
            raise ValueError("password_confirm: Passwords do not match.")
        return self


class RegisterRequest(_NewPassword):
    email: EmailStr = Field(max_length=254)
    full_name: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def _trim(self) -> Self:
        self.full_name = self.full_name.strip()
        if not self.full_name:
            raise ValueError("full_name: Enter your name.")
        return self


class LoginRequest(RequestModel):
    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX)
    totp_code: TotpCode | None = Field(
        default=None,
        description="Only for an account with two-factor sign-in: the code, to sign in in one "
        "step. Without it such an account gets a challenge (`requires_two_factor`).",
    )


class TwoFactorValidateRequest(RequestModel):
    challenge_token: str = Field(
        min_length=20, max_length=200, description="`challenge_token` from `POST /auth/login`"
    )
    code: TotpCode


class TwoFactorCodeRequest(RequestModel):
    code: TotpCode


class TwoFactorDisableRequest(RequestModel):
    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    password: str = Field(min_length=1, max_length=PASSWORD_MAX, description="Current password")
    code: TotpCode


class EmailRequest(RequestModel):
    email: EmailStr = Field(max_length=254)


class TokenRequest(RequestModel):
    token: str = Field(min_length=20, max_length=200)


class ResetPasswordRequest(_NewPassword):
    token: str = Field(min_length=20, max_length=200)


class ChangePasswordRequest(_NewPassword):
    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int
    user: UserRead


class TwoFactorChallenge(BaseModel):
    """The password was right and the account uses two-factor sign-in: no session yet."""

    requires_two_factor: Literal[True] = Field(
        default=True, description="Always true: send the code to `POST /auth/2fa/validate`"
    )
    challenge_token: str = Field(description="Answer it with a code within `expires_in` seconds")
    expires_in: int = Field(description="Seconds the challenge stays valid")


class TwoFactorSetup(BaseModel):
    secret: str = Field(
        description="Base32 secret for manual entry in the authenticator app; shown once"
    )
    otpauth_uri: str = Field(description="`otpauth://totp/...` URI to show as a QR code")


class RevokedCount(BaseModel):
    revoked: int

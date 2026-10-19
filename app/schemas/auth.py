from typing import Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.core.security import COMMON_PASSWORDS
from app.schemas.common import RequestModel
from app.schemas.user import UserRead

PASSWORD_MIN, PASSWORD_MAX = 10, 128


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


class RevokedCount(BaseModel):
    revoked: int

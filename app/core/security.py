"""Password hashing and opaque token helpers."""

import asyncio
import hashlib
import secrets

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

_hasher = PasswordHash((Argon2Hasher(),))
# Verified when the email is unknown, so response time does not reveal whether an account exists.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")

# A short list of the most common passwords of acceptable length; longer lists belong in a file.
COMMON_PASSWORDS = frozenset(
    {
        "1234567890",
        "12345678910",
        "123456789a",
        "qwertyuiop",
        "password12",
        "password123",
        "password1234",
        "1q2w3e4r5t",
        "1qaz2wsx3edc",
        "qwerty12345",
        "iloveyou123",
        "letmein123",
        "welcome123",
        "administrator",
        "passw0rd123",
        "0987654321",
        "abcdefghij",
        "abc1234567",
        "qwerty123456",
        "changeme123",
    }
)


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password: str, password_hash: str | None) -> tuple[bool, str | None]:
    """Return ``(valid, upgraded_hash)``. ``upgraded_hash`` is set when parameters changed."""
    valid, new_hash = await asyncio.to_thread(
        _hasher.verify_and_update, password, password_hash or _DUMMY_HASH
    )
    if password_hash is None:
        return False, None
    return valid, new_hash


def generate_token() -> str:
    """A 256-bit URL-safe random token."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hex digest. Tokens are high-entropy, so a fast hash is appropriate."""
    return hashlib.sha256(token.encode()).hexdigest()


_PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # noqa: S105


def generate_password(length: int = 16) -> str:
    """A random password that is easy to read out: no 0/O, 1/l/I. About 5.7 bits a character.

    Always holds a lower-case letter, an upper-case letter and a digit.
    """
    while True:
        password = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
        ):
            return password

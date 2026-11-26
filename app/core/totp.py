"""Two-factor sign-in codes (TOTP, RFC 6238) and the encryption of their secrets at rest.

* A secret is 32 base32 characters (160 bits), the format every authenticator app reads from the
  ``otpauth://`` URI.
* A code belongs to a 30-second step. One step either side of now is accepted (the phone's clock
  may drift), but never a step at or before the last one accepted for the account, so a code can
  never be used twice, nor an older code after a newer one.
* Secrets are stored encrypted with Fernet (``MFA_ENCRYPTION_KEY``). Outside production an empty
  key is derived from ``JWT_SECRET_KEY`` so development needs no extra setting; production
  refuses to start without the key (``Settings._check_production``).
"""

import base64
import hashlib
import hmac
from datetime import datetime
from functools import lru_cache

import pyotp
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings

STEP_SECONDS = 30
# Steps accepted either side of the current one.
DRIFT_STEPS = 1
CODE_LENGTH = 6


class SecretUnreadableError(Exception):
    """A stored secret cannot be decrypted (the key changed): two-factor must be reset."""


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    """The ``otpauth://totp/...`` URI an authenticator app scans (as a QR code) or reads."""
    return pyotp.TOTP(secret, digits=CODE_LENGTH, interval=STEP_SECONDS).provisioning_uri(
        name=account, issuer_name=issuer
    )


def current_step(now: datetime) -> int:
    return int(now.timestamp()) // STEP_SECONDS


def matching_step(secret: str, code: str, *, now: datetime, last_step: int | None) -> int | None:
    """The step ``code`` is valid for (now, or ``DRIFT_STEPS`` either side), or None. Steps at or
    before ``last_step`` are never accepted."""
    if len(code) != CODE_LENGTH or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret, digits=CODE_LENGTH, interval=STEP_SECONDS)
    now_step = current_step(now)
    for step in range(now_step - DRIFT_STEPS, now_step + DRIFT_STEPS + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp.at(step * STEP_SECONDS), code):
            return step
    return None


@lru_cache(maxsize=4)
def _fernet(configured: str, jwt_secret: str) -> Fernet:
    if configured:
        return Fernet(configured.encode())
    derived = hashlib.sha256(b"capium-time-fees:mfa:" + jwt_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def _cipher(settings: Settings) -> Fernet:
    return _fernet(
        settings.mfa_encryption_key.get_secret_value().strip(),
        settings.jwt_secret_key.get_secret_value(),
    )


def encrypt_secret(settings: Settings, secret: str) -> str:
    return _cipher(settings).encrypt(secret.encode()).decode()


def decrypt_secret(settings: Settings, stored: str) -> str:
    try:
        return _cipher(settings).decrypt(stored.encode()).decode()
    except InvalidToken as exc:
        raise SecretUnreadableError from exc

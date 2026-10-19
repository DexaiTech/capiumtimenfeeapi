"""Choosing the gateway."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.session import get_sessionmaker
from app.services.messaging.base import (
    Attachment,
    DeliveryResult,
    MessageGateway,
    OutboundEmail,
    OutboundSms,
    PermanentSendError,
)
from app.services.messaging.mock import MockGateway
from app.services.messaging.smtp import SmtpGateway

__all__ = [
    "Attachment",
    "DeliveryResult",
    "MessageGateway",
    "MockGateway",
    "OutboundEmail",
    "OutboundSms",
    "PermanentSendError",
    "SmtpGateway",
    "build_gateway",
]


def build_gateway(
    settings: Settings, sessionmaker: async_sessionmaker[AsyncSession] | None = None
) -> MessageGateway:
    """The gateway of ``MESSAGING_BACKEND`` (smtp when SMTP_HOST is set, else mock). SMS always
    goes to the mock."""
    mock = MockGateway(sessionmaker or get_sessionmaker())
    if settings.effective_messaging_backend == "smtp":
        return SmtpGateway(settings, sms=mock)
    return mock

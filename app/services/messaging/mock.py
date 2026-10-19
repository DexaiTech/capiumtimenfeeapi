"""The mock gateway: records every message in ``message_log`` instead of calling a provider.

It is the email gateway when no SMTP server is configured (or ``MESSAGING_BACKEND=mock``) and
the only SMS gateway. Each message is written in a transaction of its own, so the record of a
failed attempt survives the rollback of the job that made it. One-time links are removed from
the stored text.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models import MessageLog
from app.services.messaging.base import DeliveryResult, OutboundEmail, OutboundSms
from app.utils.ids import new_id

log = get_logger(__name__)

REMOVED = "[removed]"


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REMOVED)
    return text


class MockGateway:
    name = "mock"

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def send_email(self, message: OutboundEmail) -> DeliveryResult:
        return await self._send(
            MessageLog(
                id=new_id(),
                organization_id=message.organization_id,
                user_id=message.user_id,
                channel="email",
                recipient=message.to,
                subject=_redact(message.subject, message.secrets),
                body=_redact(message.text, message.secrets),
                html_body=_redact(message.html, message.secrets),
                template_key=message.template,
            )
        )

    async def send_sms(self, message: OutboundSms) -> DeliveryResult:
        return await self._send(
            MessageLog(
                id=new_id(),
                organization_id=message.organization_id,
                user_id=message.user_id,
                channel="sms",
                recipient=message.to,
                body=message.body,
                template_key=message.template,
            )
        )

    async def _send(self, entry: MessageLog) -> DeliveryResult:
        try:
            provider_ref = await self._call_provider(entry.channel, entry.recipient)
        except Exception as exc:
            entry.status = "failed"
            entry.error = f"{type(exc).__name__}: {exc}"[:500]
            await self._record(entry)
            log.warning("message_failed", channel=entry.channel, template=entry.template_key)
            raise
        entry.status = "sent"
        entry.provider_ref = provider_ref
        await self._record(entry)
        log.info("message_recorded", channel=entry.channel, template=entry.template_key)
        return DeliveryResult(provider_ref=provider_ref)

    async def _call_provider(self, channel: str, recipient: str) -> str:
        """Where a real provider would be called. The mock accepts everything."""
        return f"mock-{uuid.uuid4().hex[:16]}"

    async def _record(self, entry: MessageLog) -> None:
        async with self._sessionmaker() as session:
            session.add(entry)
            await session.commit()

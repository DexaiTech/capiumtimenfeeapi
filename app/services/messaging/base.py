"""The shared messaging service: one gateway interface for email and SMS.

Everything the worker sends, emails and SMS alike, goes through a :class:`MessageGateway`.
``SmtpGateway`` is the real email sender; ``MockGateway`` records messages in ``message_log``
instead of calling a provider, and is the only SMS implementation (no real SMS provider
exists). ``MESSAGING_BACKEND`` chooses the email gateway (see ``app.core.config``).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    content: bytes
    mime_type: str = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    to: str
    subject: str
    html: str
    text: str
    # The template key, for the log.
    template: str
    attachments: Sequence[Attachment] = ()
    organization_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    # Text a gateway that records messages must not keep: one-time links.
    secrets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OutboundSms:
    to: str
    body: str
    template: str
    organization_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """What the provider answered: its reference for the message, when it gives one."""

    provider_ref: str | None = None


class PermanentSendError(Exception):
    """The message can never be delivered (for example the recipient was rejected): the job
    fails at once instead of being retried."""


class MessageGateway(Protocol):
    """A provider of email and SMS. A transient failure raises any other exception: the job
    is then retried with backoff."""

    name: str

    async def send_email(self, message: OutboundEmail) -> DeliveryResult: ...

    async def send_sms(self, message: OutboundSms) -> DeliveryResult: ...

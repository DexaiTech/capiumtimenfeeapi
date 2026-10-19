"""The real email gateway: SMTP. SMS is delegated to the mock gateway (no provider exists)."""

from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from urllib.parse import urlsplit

import aiosmtplib

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.messaging.base import (
    DeliveryResult,
    MessageGateway,
    OutboundEmail,
    OutboundSms,
    PermanentSendError,
)
from app.services.template_engine import one_line

log = get_logger(__name__)


class SmtpGateway:
    name = "smtp"

    def __init__(self, settings: Settings, sms: MessageGateway) -> None:
        self.settings = settings
        self._sms = sms

    async def send_sms(self, message: OutboundSms) -> DeliveryResult:
        return await self._sms.send_sms(message)

    async def send_email(self, message: OutboundEmail) -> DeliveryResult:
        settings = self.settings
        email = EmailMessage()
        email["From"] = formataddr((one_line(settings.smtp_from_name), settings.smtp_from_address))
        email["To"] = one_line(message.to)
        email["Subject"] = message.subject
        email["Message-ID"] = make_msgid(domain=settings.smtp_from_address.rpartition("@")[2])
        email.set_content(message.text)
        email.add_alternative(message.html, subtype="html")
        for attachment in message.attachments:
            maintype, _, subtype = attachment.mime_type.partition("/")
            email.add_attachment(
                attachment.content,
                maintype=maintype,
                subtype=subtype or "octet-stream",
                filename=one_line(attachment.filename),
            )

        domain = message.to.rpartition("@")[2]
        template = message.template
        try:
            await aiosmtplib.send(
                email,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user or None,
                password=settings.smtp_password.get_secret_value() or None,
                use_tls=settings.smtp_port == 465,
                timeout=settings.smtp_timeout_seconds,
                # Without this aiosmtplib resolves the machine's FQDN, which can block for seconds.
                local_hostname=urlsplit(settings.backend_url).hostname or "localhost",
            )
        except aiosmtplib.SMTPRecipientsRefused as exc:
            log.warning("email_rejected", template=template, recipient_domain=domain)
            raise PermanentSendError("recipient refused") from exc
        except aiosmtplib.SMTPResponseException as exc:
            log.warning(
                "email_failed", template=template, recipient_domain=domain, smtp_code=exc.code
            )
            if 500 <= exc.code < 600 and exc.code not in (535, 530):
                raise PermanentSendError(f"smtp {exc.code}") from exc
            raise
        # The log never contains the body or the link.
        log.info("email_sent", template=template, recipient_domain=domain)
        return DeliveryResult(provider_ref=str(email["Message-ID"]))

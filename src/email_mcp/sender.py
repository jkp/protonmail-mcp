"""Async SMTP sending via aiosmtplib."""

from email.message import EmailMessage
from pathlib import Path

import aiosmtplib
import structlog

logger = structlog.get_logger()


class SmtpSender:
    """Send email messages via SMTP."""

    def __init__(
        self,
        hostname: str = "127.0.0.1",
        port: int = 1025,
        username: str = "",
        password: str = "",
        start_tls: bool = False,
    ) -> None:
        self.hostname = hostname
        self.port = port
        self.username = username
        self.password = password
        self.start_tls = start_tls

    async def send(self, message: EmailMessage) -> None:
        """Send an email message via SMTP."""
        logger.info(
            "smtp.sending",
            to=message.get("To", ""),
            subject=message.get("Subject", ""),
        )
        await aiosmtplib.send(
            message,
            hostname=self.hostname,
            port=self.port,
            start_tls=self.start_tls,
            username=self.username or None,
            password=self.password or None,
        )
        logger.info("smtp.sent", to=message.get("To", ""))

    async def send_and_save(
        self, message: EmailMessage, maildir_root: Path, folder: str = "Sent"
    ) -> None:
        """Send an email and save a copy to the Sent folder."""
        await self.send(message)

        # Save to Sent Maildir
        from email_mcp.store import MaildirStore

        store = MaildirStore(maildir_root)
        store.save_message(folder, message.as_bytes())

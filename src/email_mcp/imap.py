"""IMAP mutator: execute mutations directly on the IMAP server.

Uses IMAPClient (sync) wrapped in asyncio.to_thread() for async access.
aioimaplib lacks STARTTLS support which ProtonMail Bridge requires.
"""

import asyncio
import ssl

import structlog
from imapclient import IMAPClient

logger = structlog.get_logger()


class ImapError(Exception):
    """Error during IMAP operations."""


class ImapMutator:
    """Execute move/delete/flag mutations directly on IMAP."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 1143,
        username: str = "",
        password: str = "",
        starttls: bool = True,
        cert_path: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.starttls = starttls
        self.cert_path = cert_path
        self._client: IMAPClient | None = None
        self._tls_context: ssl.SSLContext | None = None
        if cert_path:
            import os

            self._tls_context = ssl.create_default_context(
                cafile=os.path.expanduser(cert_path)
            )

    def _connect_sync(self) -> IMAPClient:
        """Synchronous IMAP connect + login."""
        client = IMAPClient(self.host, port=self.port, ssl=False)
        if self.starttls:
            ctx = self._tls_context or ssl.create_default_context()
            client.starttls(ssl_context=ctx)
        client.login(self.username, self.password)
        return client

    async def connect(self) -> None:
        """Establish IMAP connection with optional STARTTLS."""
        self._client = await asyncio.to_thread(self._connect_sync)
        logger.info("imap.connected", host=self.host, port=self.port)

    async def disconnect(self) -> None:
        """Close the IMAP connection."""
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.logout)
            except Exception:
                logger.debug("imap.logout_failed", exc_info=True)
            self._client = None

    async def _ensure_connected(self) -> None:
        """Reconnect if the connection is lost or dead."""
        if self._client is None:
            await self.connect()
            return
        # Check if connection is still alive with a NOOP
        try:
            await asyncio.to_thread(lambda: self._client.noop())
        except Exception:
            logger.debug("imap.connection_dead, reconnecting")
            self._client = None
            await self.connect()

    def _find_uid_sync(
        self, message_id: str, folder: str | None = None
    ) -> tuple[str, int]:
        """Find the UID and folder for a message by Message-ID (sync)."""
        assert self._client is not None
        normalized = message_id.strip().strip("<>")
        criteria = [b"HEADER", b"Message-ID", f"<{normalized}>".encode()]

        if folder:
            folders = [folder]
        else:
            folders = ["INBOX"]
            for flags, delimiter, name in self._client.list_folders():
                name_str = name if isinstance(name, str) else name.decode()
                if name_str not in folders:
                    folders.append(name_str)

        for f in folders:
            self._client.select_folder(f, readonly=True)
            uids = self._client.search(criteria)
            if uids:
                return f, uids[0]

        raise ImapError(f"Message not found: {message_id}")

    async def _find_uid(
        self, message_id: str, folder: str | None = None
    ) -> tuple[str, int]:
        """Find the UID and folder for a message by Message-ID."""
        await self._ensure_connected()
        return await asyncio.to_thread(
            self._find_uid_sync, message_id, folder
        )

    def _move_sync(
        self, message_id: str, to_folder: str, from_folder: str | None = None
    ) -> None:
        """Move a message: SELECT → SEARCH → COPY → DELETE → EXPUNGE (sync)."""
        assert self._client is not None
        folder, uid = self._find_uid_sync(message_id, from_folder)
        # Re-select writable
        self._client.select_folder(folder)
        self._client.copy([uid], to_folder)
        self._client.delete_messages([uid])
        self._client.expunge([uid])
        logger.info(
            "imap.moved",
            message_id=message_id,
            from_folder=folder,
            to_folder=to_folder,
        )

    async def move(
        self,
        message_id: str,
        to_folder: str,
        from_folder: str | None = None,
    ) -> None:
        """Move a message by COPY + DELETE + EXPUNGE."""
        await self._ensure_connected()
        try:
            await asyncio.to_thread(
                self._move_sync, message_id, to_folder, from_folder
            )
        except ImapError:
            raise
        except Exception as e:
            raise ImapError(str(e)) from e

    async def delete(
        self, message_id: str, from_folder: str | None = None
    ) -> None:
        """Move a message to Trash."""
        await self.move(message_id, "Trash", from_folder)

    async def archive(
        self, message_id: str, from_folder: str | None = None
    ) -> None:
        """Move a message to Archive."""
        await self.move(message_id, "Archive", from_folder)

    def _set_flags_sync(
        self, message_id: str, flags: list[str], folder: str | None = None
    ) -> None:
        """Set flags on a message (sync)."""
        assert self._client is not None
        found_folder, uid = self._find_uid_sync(message_id, folder)
        self._client.select_folder(found_folder)
        self._client.set_flags([uid], flags)

    def _add_flags_sync(
        self, message_id: str, flags: list[str], folder: str | None = None
    ) -> None:
        """Add flags to a message (sync)."""
        assert self._client is not None
        found_folder, uid = self._find_uid_sync(message_id, folder)
        self._client.select_folder(found_folder)
        self._client.add_flags([uid], flags)

    def _remove_flags_sync(
        self, message_id: str, flags: list[str], folder: str | None = None
    ) -> None:
        """Remove flags from a message (sync)."""
        assert self._client is not None
        found_folder, uid = self._find_uid_sync(message_id, folder)
        self._client.select_folder(found_folder)
        self._client.remove_flags([uid], flags)

    async def set_flags(
        self, message_id: str, flags: str, folder: str | None = None
    ) -> None:
        """Set flags on a message (replaces existing flags)."""
        await self._ensure_connected()
        flag_list = flags.split()
        await asyncio.to_thread(
            self._set_flags_sync, message_id, flag_list, folder
        )

    async def add_flags(
        self, message_id: str, flags: str, folder: str | None = None
    ) -> None:
        """Add flags to a message."""
        await self._ensure_connected()
        flag_list = flags.split()
        await asyncio.to_thread(
            self._add_flags_sync, message_id, flag_list, folder
        )

    async def remove_flags(
        self, message_id: str, flags: str, folder: str | None = None
    ) -> None:
        """Remove flags from a message."""
        await self._ensure_connected()
        flag_list = flags.split()
        await asyncio.to_thread(
            self._remove_flags_sync, message_id, flag_list, folder
        )

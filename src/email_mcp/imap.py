"""IMAP mutator: execute mutations directly on the IMAP server.

Uses IMAPClient (sync) wrapped in asyncio.to_thread() for async access.
aioimaplib lacks STARTTLS support which ProtonMail Bridge requires.
"""

import asyncio
import ssl
import time

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

    # ── Batch operations ──────────────────────────────────────────────

    def _batch_find_uids_sync(
        self, message_ids: list[str], folder: str | None = None
    ) -> tuple[dict[str, list[tuple[str, int]]], list[dict]]:
        """Find UIDs for multiple messages, grouped by folder.

        Returns:
            (uids_by_folder, errors) where uids_by_folder is
            {folder: [(message_id, uid), ...]} and errors is
            [{"message_id": ..., "detail": ...}, ...]
        """
        assert self._client is not None
        uids_by_folder: dict[str, list[tuple[str, int]]] = {}
        errors: list[dict] = []

        if folder:
            folders = [folder]
        else:
            folders = ["INBOX"]
            for _flags, _delimiter, name in self._client.list_folders():
                name_str = name if isinstance(name, str) else name.decode()
                if name_str not in folders:
                    folders.append(name_str)

        # Build criteria for all messages upfront
        remaining: dict[str, bytes] = {}
        for message_id in message_ids:
            normalized = message_id.strip().strip("<>")
            remaining[message_id] = f"<{normalized}>".encode()

        t0 = time.monotonic()
        total_searches = 0

        # Iterate folders in outer loop: O(F) SELECTs instead of O(N*F)
        for f in folders:
            if not remaining:
                break
            self._client.select_folder(f, readonly=True)
            folder_found = 0
            for message_id, encoded_id in list(remaining.items()):
                criteria = [b"HEADER", b"Message-ID", encoded_id]
                uids = self._client.search(criteria)
                total_searches += 1
                if uids:
                    uids_by_folder.setdefault(f, []).append(
                        (message_id, uids[0])
                    )
                    del remaining[message_id]
                    folder_found += 1
            if folder_found:
                logger.debug(
                    "imap.uid_resolve_folder",
                    folder=f,
                    found=folder_found,
                    searched=folder_found + len(remaining),
                )

        elapsed = time.monotonic() - t0
        logger.info(
            "imap.uid_resolve_done",
            total=len(message_ids),
            resolved=len(message_ids) - len(remaining),
            not_found=len(remaining),
            searches=total_searches,
            elapsed_s=round(elapsed, 2),
        )

        location = folder if folder else "any folder"
        for message_id in remaining:
            errors.append(
                {"message_id": message_id, "reason": f"not found in {location}"}
            )

        return uids_by_folder, errors

    async def _batch_find_uids(
        self, message_ids: list[str], folder: str | None = None
    ) -> dict[str, list[tuple[str, int]]]:
        """Find UIDs for multiple messages, grouped by folder.

        Raises ImapError if any message is not found.
        """
        await self._ensure_connected()
        uids_by_folder, errors = await asyncio.to_thread(
            self._batch_find_uids_sync, message_ids, folder
        )
        if errors:
            raise ImapError(
                f"Messages not found: {[e['message_id'] for e in errors]}"
            )
        return uids_by_folder

    async def _batch_find_uids_with_errors(
        self, message_ids: list[str], folder: str | None = None
    ) -> tuple[dict[str, list[tuple[str, int]]], list[dict]]:
        """Find UIDs for multiple messages, returning errors instead of raising."""
        await self._ensure_connected()
        return await asyncio.to_thread(
            self._batch_find_uids_sync, message_ids, folder
        )

    def _batch_move_sync(
        self,
        uids_by_folder: dict[str, list[tuple[str, int]]],
        to_folder: str,
    ) -> tuple[int, list[dict]]:
        """Batch COPY+DELETE+EXPUNGE per folder.

        Returns (succeeded_count, errors).
        """
        assert self._client is not None
        succeeded = 0
        errors: list[dict] = []

        for folder, entries in uids_by_folder.items():
            uids = [uid for _, uid in entries]
            msg_ids = [mid for mid, _ in entries]
            try:
                self._client.select_folder(folder)
                t0 = time.monotonic()
                self._client.copy(uids, to_folder)
                t_copy = time.monotonic() - t0
                self._client.delete_messages(uids)
                t_delete = time.monotonic() - t0 - t_copy
                self._client.expunge(uids)
                t_expunge = time.monotonic() - t0 - t_copy - t_delete
                succeeded += len(entries)
                logger.info(
                    "imap.batch_moved",
                    count=len(entries),
                    from_folder=folder,
                    to_folder=to_folder,
                    copy_s=round(t_copy, 2),
                    delete_s=round(t_delete, 2),
                    expunge_s=round(t_expunge, 2),
                    total_s=round(time.monotonic() - t0, 2),
                )
            except Exception as e:
                for mid in msg_ids:
                    errors.append({"message_id": mid, "reason": str(e)})

        return succeeded, errors

    def _batch_add_flags_sync(
        self,
        uids_by_folder: dict[str, list[tuple[str, int]]],
        flags: list[str],
    ) -> tuple[int, list[dict]]:
        """Batch STORE flags per folder.

        Returns (succeeded_count, errors).
        """
        assert self._client is not None
        succeeded = 0
        errors: list[dict] = []

        for folder, entries in uids_by_folder.items():
            uids = [uid for _, uid in entries]
            msg_ids = [mid for mid, _ in entries]
            try:
                self._client.select_folder(folder)
                t0 = time.monotonic()
                self._client.add_flags(uids, flags)
                elapsed = time.monotonic() - t0
                succeeded += len(entries)
                logger.info(
                    "imap.batch_flags_added",
                    count=len(entries),
                    folder=folder,
                    flags=flags,
                    elapsed_s=round(elapsed, 2),
                )
            except Exception as e:
                for mid in msg_ids:
                    errors.append({"message_id": mid, "reason": str(e)})

        return succeeded, errors

    async def batch_move(
        self,
        message_ids: list[str],
        to_folder: str,
        from_folder: str | None = None,
    ) -> tuple[int, list[dict]]:
        """Batch move messages by COPY + DELETE + EXPUNGE, grouped by folder."""
        await self._ensure_connected()
        uids_by_folder, find_errors = await asyncio.to_thread(
            self._batch_find_uids_sync, message_ids, from_folder
        )
        if not uids_by_folder:
            return 0, find_errors
        succeeded, move_errors = await asyncio.to_thread(
            self._batch_move_sync, uids_by_folder, to_folder
        )
        return succeeded, find_errors + move_errors

    async def batch_archive(
        self,
        message_ids: list[str],
        from_folder: str | None = None,
    ) -> tuple[int, list[dict]]:
        """Batch move messages to Archive."""
        return await self.batch_move(message_ids, "Archive", from_folder)

    async def batch_delete(
        self,
        message_ids: list[str],
        from_folder: str | None = None,
    ) -> tuple[int, list[dict]]:
        """Batch move messages to Trash."""
        return await self.batch_move(message_ids, "Trash", from_folder)

    async def batch_add_flags(
        self,
        message_ids: list[str],
        flags: str,
        folder: str | None = None,
    ) -> tuple[int, list[dict]]:
        """Batch add flags to messages."""
        await self._ensure_connected()
        flag_list = flags.split()
        uids_by_folder, find_errors = await asyncio.to_thread(
            self._batch_find_uids_sync, message_ids, folder
        )
        if not uids_by_folder:
            return 0, find_errors
        succeeded, flag_errors = await asyncio.to_thread(
            self._batch_add_flags_sync, uids_by_folder, flag_list
        )
        return succeeded, find_errors + flag_errors

    # ── Pre-grouped batch operations (query-based) ────────────────────

    async def batch_move_by_folder(
        self,
        message_ids_by_folder: dict[str, list[str]],
        to_folder: str,
    ) -> tuple[int, list[dict]]:
        """Batch move with pre-resolved folders.

        Accepts {folder: [message_id, ...]} so each group only searches
        one IMAP folder (fast). A message may appear in multiple folder
        groups if it exists in multiple folders (e.g. self-sent emails).
        Returns (succeeded, errors) with reasons.
        """
        t_start = time.monotonic()
        total_messages = sum(len(ids) for ids in message_ids_by_folder.values())
        logger.info(
            "imap.batch_move_by_folder.start",
            folders=list(message_ids_by_folder.keys()),
            total_messages=total_messages,
            to_folder=to_folder,
        )

        await self._ensure_connected()
        all_uids: dict[str, list[tuple[str, int]]] = {}
        all_errors: list[dict] = []

        for folder, message_ids in message_ids_by_folder.items():
            uids_by_folder, errors = await asyncio.to_thread(
                self._batch_find_uids_sync, message_ids, folder
            )
            for f, entries in uids_by_folder.items():
                all_uids.setdefault(f, []).extend(entries)
            all_errors.extend(errors)

        if not all_uids:
            return 0, all_errors

        succeeded, move_errors = await asyncio.to_thread(
            self._batch_move_sync, all_uids, to_folder
        )

        logger.info(
            "imap.batch_move_by_folder.done",
            succeeded=succeeded,
            failed=len(all_errors) + len(move_errors),
            total_s=round(time.monotonic() - t_start, 2),
        )
        return succeeded, all_errors + move_errors

    async def batch_add_flags_by_folder(
        self,
        message_ids_by_folder: dict[str, list[str]],
        flags: list[str],
    ) -> tuple[int, list[dict]]:
        """Batch add flags with pre-resolved folders.

        Accepts {folder: [message_id, ...]} so each group only searches
        one IMAP folder (fast). A message may appear in multiple folder
        groups if it exists in multiple folders (e.g. self-sent emails).
        Returns (succeeded, errors) with reasons.
        """
        t_start = time.monotonic()
        total_messages = sum(len(ids) for ids in message_ids_by_folder.values())
        logger.info(
            "imap.batch_add_flags_by_folder.start",
            folders=list(message_ids_by_folder.keys()),
            total_messages=total_messages,
            flags=flags,
        )

        await self._ensure_connected()
        all_uids: dict[str, list[tuple[str, int]]] = {}
        all_errors: list[dict] = []

        for folder, message_ids in message_ids_by_folder.items():
            uids_by_folder, errors = await asyncio.to_thread(
                self._batch_find_uids_sync, message_ids, folder
            )
            for f, entries in uids_by_folder.items():
                all_uids.setdefault(f, []).extend(entries)
            all_errors.extend(errors)

        if not all_uids:
            return 0, all_errors

        succeeded, flag_errors = await asyncio.to_thread(
            self._batch_add_flags_sync, all_uids, flags
        )

        logger.info(
            "imap.batch_add_flags_by_folder.done",
            succeeded=succeeded,
            failed=len(all_errors) + len(flag_errors),
            total_s=round(time.monotonic() - t_start, 2),
        )
        return succeeded, all_errors + flag_errors

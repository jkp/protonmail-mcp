"""ProtonMail message decryptor — fetches encrypted bodies from API and decrypts.

Replaces the Bridge IMAP body-fetching path. Messages are fetched by pm_id
directly from the ProtonMail API, then decrypted using the ProtonKeyRing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from email_mcp.crypto import DecryptionError, ProtonKeyRing
from email_mcp.proton_api import ProtonClient

logger = structlog.get_logger(__name__)


class ProtonDecryptor:
    """Fetch and decrypt ProtonMail message bodies via the REST API.

    Usage:
        decryptor = ProtonDecryptor(api, key_ring)
        body, attachments = await decryptor.fetch_and_decrypt(pm_id)
        results = await decryptor.fetch_and_decrypt_batch(pm_ids)
    """

    def __init__(self, api: ProtonClient, key_ring: ProtonKeyRing) -> None:
        self._api = api
        self._key_ring = key_ring

    async def fetch_and_decrypt(self, pm_id: str) -> tuple[str, list[dict[str, Any]]]:
        """Fetch a single message and decrypt its body.

        Returns:
            (plaintext_body, attachments) where attachments is a list of
            {att_id, filename, size, mime_type} dicts.
        """
        msg = await self._api.get_message(pm_id)
        body = msg.get("Body", "")

        if not body:
            return "", self._extract_attachments(msg)

        try:
            plaintext = self._key_ring.decrypt(body)
        except DecryptionError:
            logger.warning("decryptor.decrypt_failed", pm_id=pm_id)
            raise

        return plaintext, self._extract_attachments(msg)

    async def fetch_and_decrypt_batch(
        self,
        pm_ids: list[str],
        concurrency: int = 10,
    ) -> dict[str, tuple[str, list[dict[str, Any]]]]:
        """Fetch and decrypt multiple messages in parallel.

        Uses asyncio.Semaphore to limit concurrent API calls.

        Returns:
            {pm_id: (plaintext_body, attachments)} for successfully decrypted messages.
            Failed messages are logged and omitted from results.
        """
        sem = asyncio.Semaphore(concurrency)
        results: dict[str, tuple[str, list[dict[str, Any]]]] = {}

        async def _fetch_one(pm_id: str) -> None:
            async with sem:
                try:
                    body, atts = await self.fetch_and_decrypt(pm_id)
                    results[pm_id] = (body, atts)
                except Exception as e:
                    logger.warning("decryptor.batch_failed", pm_id=pm_id, error=str(e))

        await asyncio.gather(*[_fetch_one(pid) for pid in pm_ids])
        return results

    async def fetch_attachment(self, att_id: str, key_packets_b64: str) -> bytes:
        """Fetch and decrypt an attachment by its ProtonMail attachment ID.

        Args:
            att_id: ProtonMail attachment ID.
            key_packets_b64: Base64-encoded encrypted session key packets.

        Returns:
            Decrypted attachment bytes.
        """
        encrypted = await self._api.get_attachment(att_id)

        # The attachment is a PGP message encrypted with the session key.
        # First decrypt the session key from KeyPackets, then use it to
        # decrypt the attachment body.
        try:
            return self._key_ring.decrypt_binary(encrypted)
        except Exception:
            logger.warning("decryptor.attachment_decrypt_failed", att_id=att_id)
            raise

    @staticmethod
    def _extract_attachments(msg: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract attachment metadata from API message response."""
        attachments = []
        for att in msg.get("Attachments", []):
            attachments.append({
                "att_id": att["ID"],
                "filename": att.get("Name", ""),
                "size": att.get("Size", 0),
                "mime_type": att.get("MIMEType", "application/octet-stream"),
                "key_packets": att.get("KeyPackets", ""),
            })
        return attachments

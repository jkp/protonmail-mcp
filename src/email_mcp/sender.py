"""Send email via ProtonMail API (no SMTP, no Bridge)."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

import pgpy
import structlog

from email_mcp.proton_api import ProtonClient

if TYPE_CHECKING:
    from email_mcp.crypto import ProtonKeyRing

logger = structlog.get_logger(__name__)


def _split_pgp_packets(encrypted: pgpy.PGPMessage) -> tuple[bytes, bytes]:
    """Split an encrypted PGP message into key packets and data packets.

    ProtonMail's send API requires these separated:
    - Key packets (PKESK): encrypted session key, per-recipient
    - Data packets (SEIPD): the encrypted body, shared

    Returns (key_packets_raw, data_packets_raw).
    """
    enc_bytes = bytes(encrypted)
    pos = 0
    key_end = 0

    while pos < len(enc_bytes):
        tag_byte = enc_bytes[pos]
        if not (tag_byte & 0x80):
            break

        if tag_byte & 0x40:  # New format
            tag = tag_byte & 0x3F
            pos += 1
            if enc_bytes[pos] < 192:
                length = enc_bytes[pos]
                pos += 1
            elif enc_bytes[pos] < 224:
                length = ((enc_bytes[pos] - 192) << 8) + enc_bytes[pos + 1] + 192
                pos += 2
            else:
                length = int.from_bytes(enc_bytes[pos + 1 : pos + 5], "big")
                pos += 5
        else:  # Old format
            tag = (tag_byte & 0x3C) >> 2
            lt = tag_byte & 0x03
            pos += 1
            if lt == 0:
                length = enc_bytes[pos]
                pos += 1
            elif lt == 1:
                length = int.from_bytes(enc_bytes[pos : pos + 2], "big")
                pos += 2
            elif lt == 2:
                length = int.from_bytes(enc_bytes[pos : pos + 4], "big")
                pos += 4
            else:
                length = len(enc_bytes) - pos

        # Tag 18 = SEIPD, Tag 20 = AEAD — these are the data packets
        if tag in (18, 20):
            break
        key_end = pos + length
        pos += length

    return enc_bytes[:key_end], enc_bytes[key_end:]


class ProtonSender:
    """Send email via ProtonMail API.

    Flow:
    1. Encrypt body with sender's PGP public key
    2. Create draft via API (stored encrypted)
    3. Classify recipients as internal (ProtonMail) or external
    4. Send with appropriate package type per recipient
    """

    def __init__(self, api: ProtonClient, key_ring: ProtonKeyRing) -> None:
        self._api = api
        self._key_ring = key_ring
        self._addresses: list[dict[str, Any]] = []
        self._address_keys: dict[str, pgpy.PGPKey] = {}

    async def _ensure_addresses(self) -> None:
        """Load sender addresses and their public keys."""
        if self._addresses:
            return
        self._addresses = await self._api.get_addresses()
        for addr in self._addresses:
            for key in addr.get("Keys", []):
                pub, _ = pgpy.PGPKey.from_blob(key["PrivateKey"])
                self._address_keys[addr["Email"].lower()] = pub.pubkey

    def _get_address(self, from_email: str) -> tuple[dict[str, Any], pgpy.PGPKey]:
        """Find the address and public key for a sender email."""
        for addr in self._addresses:
            if addr["Email"].lower() == from_email.lower():
                pub = self._address_keys.get(from_email.lower())
                if pub:
                    return addr, pub
        raise ValueError(f"No ProtonMail address found for {from_email}")

    async def _classify_recipients(self, addresses: list[str]) -> dict[str, int]:
        """Classify recipients as internal (1) or external (2) via API key lookup."""
        result: dict[str, int] = {}
        for email in addresses:
            try:
                data = await self._api._request("GET", "/core/v4/keys", params={"Email": email})
                result[email] = data.get("RecipientType", 2)
            except Exception:
                result[email] = 2
        return result

    def _sign_encrypt_body(
        self, body_text: str, pub_key: pgpy.PGPKey, from_email: str = ""
    ) -> tuple[str, bytes, bytes]:
        """Sign body with sender key, encrypt to sender pub key, split packets.

        Returns (armored_encrypted, key_packets_raw, data_packets_raw).
        """
        pgp_msg = pgpy.PGPMessage.new(body_text)

        signing_key = self._key_ring.signing_key_for(from_email)
        with signing_key.unlock(signing_key._passphrase):  # type: ignore[attr-defined]
            pgp_msg |= signing_key.sign(pgp_msg)

        encrypted = pub_key.encrypt(pgp_msg)
        armored = str(encrypted)
        key_raw, data_raw = _split_pgp_packets(encrypted)
        return armored, key_raw, data_raw

    def _encrypt_attachment(
        self, content: bytes, pub_key: pgpy.PGPKey, from_email: str = ""
    ) -> tuple[bytes, bytes, bytes]:
        """Encrypt and sign an attachment.

        Returns (key_packets, data_packet, signature) as raw bytes.
        """
        signing_key = self._key_ring.signing_key_for(from_email)

        # Sign the plaintext
        pgp_msg = pgpy.PGPMessage.new(content, encoding=None)
        with signing_key.unlock(signing_key._passphrase):  # type: ignore[attr-defined]
            sig = signing_key.sign(pgp_msg)

        # Encrypt
        encrypted = pub_key.encrypt(pgp_msg)
        key_raw, data_raw = _split_pgp_packets(encrypted)

        return key_raw, data_raw, bytes(sig)

    async def send(
        self,
        message: EmailMessage,
        attachments: list[tuple[str, str, bytes]] | None = None,
        parent_id: str = "",
        action: int = 0,
    ) -> None:
        """Send an email via ProtonMail API.

        Args:
            message: Composed EmailMessage with headers and body.
            attachments: List of (filename, mime_type, content_bytes) to attach.
            parent_id: ProtonMail pm_id of the message being replied to/forwarded.
            action: 0=Reply, 1=ReplyAll, 2=Forward (for ProtonMail threading).
        """
        await self._ensure_addresses()

        from_addr = message.get("From", "")
        if "<" in from_addr:
            from_email = from_addr.split("<")[1].rstrip(">").strip()
        else:
            from_email = from_addr.strip()

        addr, pub_key = self._get_address(from_email)

        to_list = self._parse_recipients(message.get("To", ""))
        cc_list = self._parse_recipients(message.get("Cc", ""))
        all_recips = to_list + cc_list

        body = message.get_body(preferencelist=("plain",))
        body_text = body.get_content() if body else ""
        subject = message.get("Subject", "")

        # Sign + encrypt body (ProtonMail requires a signature on outgoing mail)
        armored, key_raw, data_raw = self._sign_encrypt_body(body_text, pub_key, from_email)
        key_b64 = base64.b64encode(key_raw).decode()
        data_b64 = base64.b64encode(data_raw).decode()

        # Classify recipients
        all_emails = [r["Address"] for r in all_recips]
        recip_types = await self._classify_recipients(all_emails)

        internal = [e for e, t in recip_types.items() if t == 1]
        external = [e for e, t in recip_types.items() if t != 1]

        logger.info(
            "proton.sending",
            to=all_emails,
            subject=subject,
            internal=len(internal),
            external=len(external),
        )

        # Create draft
        draft_msg = {
            "ToList": to_list,
            "CCList": cc_list,
            "BCCList": [],
            "Subject": subject,
            "Body": armored,
            "MIMEType": "text/plain",
            "Sender": {
                "Address": addr["Email"],
                "Name": addr.get("DisplayName", ""),
            },
            "AddressID": addr["ID"],
        }

        draft_req: dict[str, Any] = {"Message": draft_msg}

        # Threading: link to parent message for reply/forward
        if parent_id:
            draft_req["ParentID"] = parent_id
            draft_req["Action"] = action
            # ExternalID = our Message-ID (without angle brackets)
            ext_id = message.get("Message-ID", "").strip("<>")
            if ext_id:
                draft_msg["ExternalID"] = ext_id

        draft = await self._api._request(
            "POST",
            "/mail/v4/messages",
            json=draft_req,
        )
        draft_id = draft["Message"]["ID"]

        # Upload attachments to draft (if forwarding)
        if attachments:
            for filename, mime_type, content in attachments:
                key_pkt, data_pkt, sig = self._encrypt_attachment(content, pub_key, from_email)
                await self._api.upload_attachment(
                    message_id=draft_id,
                    filename=filename,
                    mime_type=mime_type,
                    key_packets=key_pkt,
                    data_packet=data_pkt,
                    signature=sig,
                )
                logger.info(
                    "proton.attachment_uploaded",
                    filename=filename,
                    size=len(content),
                )

        # Build packages — one for internal, one for external (if needed)
        packages: list[dict[str, Any]] = []

        if internal:
            int_addrs = {}
            for email in internal:
                int_addrs[email] = {
                    "Type": 1,
                    "Signature": 1,
                    "BodyKeyPacket": key_b64,
                }
            packages.append(
                {
                    "Addresses": int_addrs,
                    "Type": 1,
                    "MIMEType": "text/plain",
                    "Body": data_b64,
                }
            )

        if external:
            # Extract raw session key for cleartext delivery
            session_key, _ = self._key_ring.decrypt_session_key(key_b64)
            session_key_b64 = base64.b64encode(session_key).decode()

            ext_addrs = {}
            for email in external:
                ext_addrs[email] = {"Type": 4, "Signature": 1}
            packages.append(
                {
                    "Addresses": ext_addrs,
                    "Type": 4,
                    "MIMEType": "text/plain",
                    "Body": data_b64,
                    "BodyKey": {
                        "Key": session_key_b64,
                        "Algorithm": "aes256",
                    },
                }
            )

        # Send (delete draft on failure to avoid orphaned drafts)
        try:
            await self._api._request(
                "POST",
                f"/mail/v4/messages/{draft_id}",
                json={"Packages": packages},
            )
        except Exception:
            try:
                await self._api._request("DELETE", f"/mail/v4/messages/{draft_id}")
                logger.info("proton.draft_cleaned", draft_id=draft_id)
            except Exception:
                logger.warning("proton.draft_cleanup_failed", draft_id=draft_id)
            raise

        logger.info("proton.sent", draft_id=draft_id, subject=subject)

    @staticmethod
    def _parse_recipients(header: str) -> list[dict[str, str]]:
        """Parse an email header into ProtonMail recipient list."""
        if not header:
            return []
        recipients = []
        for part in header.split(","):
            part = part.strip()
            if "<" in part:
                name = part.split("<")[0].strip().strip('"')
                email = part.split("<")[1].rstrip(">").strip()
            else:
                name = ""
                email = part
            if email:
                recipients.append({"Name": name, "Address": email})
        return recipients

"""Tests for email_mcp.sender — ProtonMail API sending."""

import base64
from email.message import EmailMessage
from unittest.mock import AsyncMock, MagicMock, call

import pgpy
import pytest
from pgpy.constants import HashAlgorithm, KeyFlags, PubKeyAlgorithm, SymmetricKeyAlgorithm

from email_mcp.sender import ProtonSender, _split_pgp_packets

# Fake PGP packet data for tests
_FAKE_KEY_RAW = bytes([0x84, 2, 0xAA, 0xBB])
_FAKE_DATA_RAW = bytes([0xD2, 3, 0x01, 0x02, 0x03])
_FAKE_ARMORED = "-----BEGIN PGP MESSAGE-----\nfake\n-----END PGP MESSAGE-----"


@pytest.fixture
def mock_key_ring():
    kr = MagicMock()
    kr.decrypt_session_key = MagicMock(return_value=(b"\x00" * 32, MagicMock()))
    return kr


@pytest.fixture
def mock_api():
    api = AsyncMock()
    api.get_addresses = AsyncMock(
        return_value=[
            {
                "ID": "addr-123",
                "Email": "bob@protonmail.com",
                "DisplayName": "Bob",
                "Keys": [
                    {
                        "PrivateKey": "-----BEGIN PGP PRIVATE KEY BLOCK-----\nfake\n-----END PGP PRIVATE KEY BLOCK-----"
                    }
                ],
            }
        ]
    )
    return api


_BODY_SK = b"\x01" * 32
_ATT_SK = b"\x02" * 32
_FAKE_ATT_KEY_RAW = b"\xc1\x02\xaa\xbb"


@pytest.fixture(scope="module")
def recipient_key() -> pgpy.PGPKey:
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    key.add_uid(
        pgpy.PGPUID.new("Alice", email="alice@protonmail.com"),
        usage={KeyFlags.EncryptCommunications, KeyFlags.EncryptStorage},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
    )
    return key


def _internal_lookup(key: pgpy.PGPKey) -> dict:
    return {"RecipientType": 1, "Keys": [{"Flags": 3, "PublicKey": str(key.pubkey)}]}


def _session_key_in(packet_b64: str, key: pgpy.PGPKey) -> bytes:
    """Decrypt a base64 PKESK packet with the recipient's private key."""
    msg = pgpy.PGPMessage.from_blob(base64.b64decode(packet_b64))
    _, session_key = msg._sessionkeys[0].decrypt_sk(key._key)
    return bytes(session_key)


def _make_sender(mock_api, mock_key_ring):
    """Create a ProtonSender with pre-loaded addresses and mocked sign+encrypt."""
    sender = ProtonSender(api=mock_api, key_ring=mock_key_ring)
    sender._addresses = [{"ID": "addr-123", "Email": "bob@protonmail.com", "DisplayName": "Bob"}]
    mock_pub = MagicMock()
    sender._address_keys = {"bob@protonmail.com": mock_pub}
    # Mock sign+encrypt to skip PGP operations
    sender._sign_encrypt_body = MagicMock(
        return_value=(_FAKE_ARMORED, _FAKE_KEY_RAW, _FAKE_DATA_RAW)
    )
    return sender


def test_split_pgp_packets_separates_key_and_data():
    """Key packets (tag 1 PKESK) should be split from data packets (tag 18 SEIPD)."""
    pkesk = bytes([0x84, 3, 0xAA, 0xBB, 0xCC])  # old format tag=1, lt=0, len=3
    seipd = bytes([0xC0 | 18, 4, 0x01, 0x02, 0x03, 0x04])  # new format tag=18, len=4

    raw = pkesk + seipd

    class FakeMsg:
        def __bytes__(self):
            return raw

    key_raw, data_raw = _split_pgp_packets(FakeMsg())

    assert key_raw == pkesk
    assert data_raw == seipd


async def test_send_internal_recipient(mock_api, mock_key_ring, recipient_key):
    """Internal recipients get the body session key encrypted to their own key."""
    mock_key_ring.decrypt_session_key = MagicMock(return_value=(_BODY_SK, MagicMock()))
    mock_api._request = AsyncMock(
        side_effect=[
            _internal_lookup(recipient_key),  # key lookup
            {"Message": {"ID": "draft-456"}},  # create draft
            {"Code": 1000},  # send
        ]
    )

    sender = _make_sender(mock_api, mock_key_ring)

    msg = EmailMessage()
    msg["From"] = "Bob <bob@protonmail.com>"
    msg["To"] = "alice@protonmail.com"
    msg["Subject"] = "Test"
    msg.set_content("Hello")

    await sender.send(msg)

    assert mock_api._request.call_count == 3
    send_call = mock_api._request.call_args_list[2]
    pkg = send_call.kwargs["json"]["Packages"][0]
    assert pkg["Type"] == 1
    alice = pkg["Addresses"]["alice@protonmail.com"]
    assert alice["Signature"] == 1
    assert _session_key_in(alice["BodyKeyPacket"], recipient_key) == _BODY_SK


async def test_send_external_recipient(mock_api, mock_key_ring):
    """External recipients get Type 4 (ClearScheme) package with BodyKey."""
    mock_api._request = AsyncMock(
        side_effect=[
            {"RecipientType": 2},  # key lookup → external
            {"Message": {"ID": "draft-789"}},  # create draft
            {"Code": 1000},  # send
        ]
    )

    sender = _make_sender(mock_api, mock_key_ring)

    msg = EmailMessage()
    msg["From"] = "Bob <bob@protonmail.com>"
    msg["To"] = "ferdi@outlook.com"
    msg["Subject"] = "External Test"
    msg.set_content("Hello external")

    await sender.send(msg)

    send_call = mock_api._request.call_args_list[2]
    pkg = send_call.kwargs["json"]["Packages"][0]
    assert pkg["Type"] == 4
    assert pkg["BodyKey"]["Algorithm"] == "aes256"
    assert pkg["BodyKey"]["Key"] == base64.b64encode(b"\x00" * 32).decode()
    assert pkg["Addresses"]["ferdi@outlook.com"]["Signature"] == 1


async def test_send_external_with_attachments_includes_attachment_keys(mock_api, mock_key_ring):
    """Forwarding an attachment to a non-Proton recipient must populate
    AttachmentKeys on the external (Type 4) package; otherwise Proton
    rejects the send with code 2001 'Missing attachment key'."""
    mock_api._request = AsyncMock(
        side_effect=[
            {"RecipientType": 2},  # key lookup → external
            {"Message": {"ID": "draft-789"}},  # create draft
            {"Code": 1000},  # send
        ]
    )
    mock_api.upload_attachment = AsyncMock(return_value={"ID": "att-1"})

    sender = _make_sender(mock_api, mock_key_ring)
    # Skip real PGP — return deterministic fake packets per attachment
    sender._encrypt_attachment = MagicMock(
        return_value=(b"\xc1\x02\xaa\xbb", b"\xd2\x03\x01\x02\x03", b"sig")
    )

    msg = EmailMessage()
    msg["From"] = "Bob <bob@protonmail.com>"
    msg["To"] = "kirkconsulting@qbodocs.com"
    msg["Subject"] = "Receipt forward"
    msg.set_content("FYI")

    await sender.send(
        msg,
        attachments=[("receipt.pdf", "application/pdf", b"%PDF-1.4 fake")],
        action=2,
    )

    # Upload was called once
    assert mock_api.upload_attachment.await_count == 1

    send_call = mock_api._request.call_args_list[2]
    pkg = send_call.kwargs["json"]["Packages"][0]
    assert pkg["Type"] == 4
    # The fix: AttachmentKeys must be present and reference the uploaded ID
    assert "AttachmentKeys" in pkg, "external package missing AttachmentKeys → Proton 2001"
    att_keys = pkg["AttachmentKeys"]
    assert "att-1" in att_keys
    assert att_keys["att-1"]["Algorithm"] == "aes256"
    # Session key bytes round-trip through base64
    assert att_keys["att-1"]["Key"] == base64.b64encode(b"\x00" * 32).decode()


async def test_send_internal_with_attachments_emits_attachment_key_packets(
    mock_api, mock_key_ring, recipient_key
):
    """Internal recipients need each attachment's session key encrypted to
    their key (AttachmentKeyPackets); Proton rejects the send with 2001
    'Key packet missing' otherwise. AttachmentKeys is for clear (external)
    packages only."""
    mock_key_ring.decrypt_session_key = MagicMock(
        side_effect=lambda b64: (
            (_ATT_SK if base64.b64decode(b64) == _FAKE_ATT_KEY_RAW else _BODY_SK),
            MagicMock(),
        )
    )
    mock_api._request = AsyncMock(
        side_effect=[
            _internal_lookup(recipient_key),
            {"Message": {"ID": "draft-456"}},
            {"Code": 1000},
        ]
    )
    mock_api.upload_attachment = AsyncMock(return_value={"ID": "att-2"})

    sender = _make_sender(mock_api, mock_key_ring)
    sender._encrypt_attachment = MagicMock(
        return_value=(_FAKE_ATT_KEY_RAW, b"\xd2\x03\x01\x02\x03", b"sig")
    )

    msg = EmailMessage()
    msg["From"] = "Bob <bob@protonmail.com>"
    msg["To"] = "alice@protonmail.com"
    msg["Subject"] = "Internal forward"
    msg.set_content("FYI")

    await sender.send(
        msg,
        attachments=[("receipt.pdf", "application/pdf", b"%PDF-1.4 fake")],
        action=2,
    )

    send_call = mock_api._request.call_args_list[2]
    pkg = send_call.kwargs["json"]["Packages"][0]
    assert pkg["Type"] == 1
    assert "AttachmentKeys" not in pkg
    packets = pkg["Addresses"]["alice@protonmail.com"]["AttachmentKeyPackets"]
    assert set(packets) == {"att-2"}
    assert _session_key_in(packets["att-2"], recipient_key) == _ATT_SK


async def test_send_internal_recipient_without_keys_fails_before_draft(mock_api, mock_key_ring):
    """No public key for an internal recipient → refuse rather than send
    mail they cannot decrypt."""
    mock_api._request = AsyncMock(side_effect=[{"RecipientType": 1, "Keys": []}])

    sender = _make_sender(mock_api, mock_key_ring)
    msg = EmailMessage()
    msg["From"] = "Bob <bob@protonmail.com>"
    msg["To"] = "alice@protonmail.com"
    msg["Subject"] = "x"
    msg.set_content("x")

    with pytest.raises(ValueError, match="alice@protonmail.com"):
        await sender.send(msg)
    assert mock_api._request.await_count == 1


async def test_send_not_initialized_returns_error(mock_key_ring):
    """ProtonSender should fail clearly when address not found."""
    api = AsyncMock()
    api.get_addresses = AsyncMock(return_value=[])
    sender = ProtonSender(api=api, key_ring=mock_key_ring)

    msg = EmailMessage()
    msg["From"] = "nobody@protonmail.com"
    msg["To"] = "alice@example.com"
    msg.set_content("Hello")

    with pytest.raises(ValueError, match="No ProtonMail address found"):
        await sender.send(msg)


def test_parse_recipients():
    result = ProtonSender._parse_recipients('"Alice B" <alice@example.com>, bob@example.com')
    assert len(result) == 2
    assert result[0] == {"Name": "Alice B", "Address": "alice@example.com"}
    assert result[1] == {"Name": "", "Address": "bob@example.com"}


def test_parse_recipients_empty():
    assert ProtonSender._parse_recipients("") == []


def _external_msg() -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "Bob <bob@protonmail.com>"
    msg["To"] = "ferdi@outlook.com"
    msg["Subject"] = "x"
    msg.set_content("x")
    return msg


_DELETE_DRAFT = call("PUT", "/mail/v4/messages/delete", json={"IDs": ["draft-1"]})


async def test_failed_send_deletes_draft(mock_api, mock_key_ring):
    mock_api._request = AsyncMock(
        side_effect=[
            {"RecipientType": 2},
            {"Message": {"ID": "draft-1"}},
            RuntimeError("send rejected"),
            {"Code": 1001},
        ]
    )
    sender = _make_sender(mock_api, mock_key_ring)

    with pytest.raises(RuntimeError, match="send rejected"):
        await sender.send(_external_msg())

    assert mock_api._request.call_args_list[-1] == _DELETE_DRAFT


async def test_failed_attachment_upload_deletes_draft(mock_api, mock_key_ring):
    mock_api._request = AsyncMock(
        side_effect=[
            {"RecipientType": 2},
            {"Message": {"ID": "draft-1"}},
            {"Code": 1001},
        ]
    )
    mock_api.upload_attachment = AsyncMock(side_effect=RuntimeError("upload failed"))
    sender = _make_sender(mock_api, mock_key_ring)
    sender._encrypt_attachment = MagicMock(return_value=(_FAKE_ATT_KEY_RAW, b"\xd2\x01\x00", b"s"))

    with pytest.raises(RuntimeError, match="upload failed"):
        await sender.send(_external_msg(), attachments=[("a.txt", "text/plain", b"a")])

    assert mock_api._request.call_args_list[-1] == _DELETE_DRAFT


def _two_address_sender(mock_api, mock_key_ring):
    sender = _make_sender(mock_api, mock_key_ring)
    sender._addresses = [
        {"ID": "addr-alias", "Email": "alias@protonmail.com", "Order": 2, "Status": 1},
        {"ID": "addr-off", "Email": "old@protonmail.com", "Order": 0, "Status": 0},
        {"ID": "addr-main", "Email": "bob@protonmail.com", "Order": 1, "Status": 1},
    ]
    sender._address_keys = {
        "alias@protonmail.com": MagicMock(),
        "old@protonmail.com": MagicMock(),
        "bob@protonmail.com": MagicMock(),
    }
    return sender


async def test_send_without_from_uses_primary_address(mock_api, mock_key_ring):
    """No configured/explicit sender → the account's primary enabled address,
    not a failed lookup of the empty string."""
    mock_api._request = AsyncMock(
        side_effect=[{"RecipientType": 2}, {"Message": {"ID": "draft-1"}}, {"Code": 1000}]
    )
    sender = _two_address_sender(mock_api, mock_key_ring)

    msg = EmailMessage()
    msg["From"] = ""
    msg["To"] = "ferdi@outlook.com"
    msg["Subject"] = "x"
    msg.set_content("x")
    await sender.send(msg)

    draft = mock_api._request.call_args_list[1].kwargs["json"]["Message"]
    assert draft["AddressID"] == "addr-main"
    assert draft["Sender"]["Address"] == "bob@protonmail.com"


async def test_send_from_unknown_address_names_the_valid_ones(mock_api, mock_key_ring):
    sender = _two_address_sender(mock_api, mock_key_ring)

    msg = EmailMessage()
    msg["From"] = "nope@example.com"
    msg["To"] = "ferdi@outlook.com"
    msg.set_content("x")

    with pytest.raises(ValueError, match="nope@example.com") as exc:
        await sender.send(msg)
    assert "bob@protonmail.com" in str(exc.value)
    assert "alias@protonmail.com" in str(exc.value)

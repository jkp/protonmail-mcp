"""Tests for email_mcp.sender — ProtonMail API sending."""

import base64
from email.message import EmailMessage
from unittest.mock import AsyncMock, MagicMock

import pytest

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


async def test_send_internal_recipient(mock_api, mock_key_ring):
    """Internal recipients get Type 1 package with BodyKeyPacket."""
    mock_api._request = AsyncMock(
        side_effect=[
            {"RecipientType": 1},  # key lookup
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
    assert "BodyKeyPacket" in pkg["Addresses"]["alice@protonmail.com"]
    assert pkg["Addresses"]["alice@protonmail.com"]["Signature"] == 1


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

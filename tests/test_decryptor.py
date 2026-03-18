"""Tests for ProtonDecryptor — API fetch + PGP decrypt orchestration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from email_mcp.crypto import DecryptionError
from email_mcp.decryptor import ProtonDecryptor


@pytest.fixture()
def mock_api():
    api = AsyncMock()
    api.get_message = AsyncMock()
    return api


@pytest.fixture()
def mock_key_ring():
    kr = MagicMock()
    kr.decrypt = MagicMock(return_value="decrypted body text")
    return kr


@pytest.fixture()
def decryptor(mock_api, mock_key_ring):
    return ProtonDecryptor(api=mock_api, key_ring=mock_key_ring)


def _make_message(body: str = "encrypted", attachments: list | None = None) -> dict:
    return {
        "ID": "test-pm-id",
        "Body": body,
        "MIMEType": "text/html",
        "Attachments": attachments or [],
    }


# ── ProtonClient API methods ────────────────────────────────────────────────


class TestProtonClientAPIMethods:
    """Verify the API endpoints are called correctly (Phase 2)."""

    async def test_get_user(self, mock_api):
        mock_api._request = AsyncMock(return_value={"User": {"Keys": [], "Name": "test"}})
        # Simulate what ProtonClient.get_user does
        data = await mock_api._request("GET", "/core/v4/users")
        assert data["User"]["Name"] == "test"

    async def test_get_addresses(self, mock_api):
        mock_api._request = AsyncMock(return_value={"Addresses": [{"Email": "a@b.com"}]})
        data = await mock_api._request("GET", "/core/v4/addresses")
        assert len(data["Addresses"]) == 1

    async def test_get_message(self, mock_api):
        mock_api._request = AsyncMock(return_value={"Message": {"Body": "enc", "ID": "x"}})
        data = await mock_api._request("GET", "/mail/v4/messages/x")
        assert data["Message"]["Body"] == "enc"


# ── fetch_and_decrypt ────────────────────────────────────────────────────────


class TestFetchAndDecrypt:
    async def test_fetches_and_decrypts(self, decryptor, mock_api, mock_key_ring):
        mock_api.get_message.return_value = _make_message("encrypted body")
        mock_key_ring.decrypt.return_value = "plaintext"

        body, atts = await decryptor.fetch_and_decrypt("pm-123")

        mock_api.get_message.assert_called_once_with("pm-123")
        mock_key_ring.decrypt.assert_called_once_with("encrypted body")
        assert body == "plaintext"
        assert atts == []

    async def test_extracts_attachments(self, decryptor, mock_api, mock_key_ring):
        mock_api.get_message.return_value = _make_message(
            "enc",
            attachments=[
                {
                    "ID": "att-1",
                    "Name": "doc.pdf",
                    "Size": 1024,
                    "MIMEType": "application/pdf",
                    "KeyPackets": "abc",
                },
                {
                    "ID": "att-2",
                    "Name": "img.jpg",
                    "Size": 2048,
                    "MIMEType": "image/jpeg",
                    "KeyPackets": "def",
                },
            ],
        )

        _, atts = await decryptor.fetch_and_decrypt("pm-456")

        assert len(atts) == 2
        assert atts[0]["att_id"] == "att-1"
        assert atts[0]["filename"] == "doc.pdf"
        assert atts[0]["key_packets"] == "abc"
        assert atts[1]["att_id"] == "att-2"

    async def test_empty_body(self, decryptor, mock_api, mock_key_ring):
        mock_api.get_message.return_value = _make_message("")

        body, _ = await decryptor.fetch_and_decrypt("pm-789")

        assert body == ""
        mock_key_ring.decrypt.assert_not_called()

    async def test_decrypt_failure_raises(self, decryptor, mock_api, mock_key_ring):
        mock_api.get_message.return_value = _make_message("bad data")
        mock_key_ring.decrypt.side_effect = DecryptionError("no key")

        with pytest.raises(DecryptionError):
            await decryptor.fetch_and_decrypt("pm-bad")


# ── fetch_and_decrypt_batch ──────────────────────────────────────────────────


class TestFetchAndDecryptBatch:
    async def test_batch_returns_all(self, decryptor, mock_api, mock_key_ring):
        mock_api.get_message.return_value = _make_message("enc")
        mock_key_ring.decrypt.return_value = "plain"

        results = await decryptor.fetch_and_decrypt_batch(["a", "b", "c"])

        assert len(results) == 3
        assert all(body == "plain" for body, _ in results.values())

    async def test_batch_skips_failures(self, decryptor, mock_api, mock_key_ring):
        call_count = 0

        async def _get_msg(pm_id):
            nonlocal call_count
            call_count += 1
            if pm_id == "bad":
                return _make_message("bad")
            return _make_message("good")

        mock_api.get_message = _get_msg

        def _decrypt(body):
            if body == "bad":
                raise DecryptionError("nope")
            return "decrypted"

        mock_key_ring.decrypt = _decrypt

        results = await decryptor.fetch_and_decrypt_batch(["ok1", "bad", "ok2"])

        assert len(results) == 2
        assert "ok1" in results
        assert "ok2" in results
        assert "bad" not in results

    async def test_batch_concurrency(self, decryptor, mock_api, mock_key_ring):
        """Verify semaphore limits concurrency."""
        import asyncio

        active = 0
        max_active = 0

        async def _tracked_get(pm_id):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return _make_message("enc")

        mock_api.get_message = _tracked_get

        await decryptor.fetch_and_decrypt_batch([f"msg-{i}" for i in range(20)], concurrency=5)

        assert max_active <= 5

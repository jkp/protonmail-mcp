"""Tests for email_mcp.idle — IMAP IDLE listener (IMAPClient-based)."""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from email_mcp.idle import IdleListener


@pytest.fixture
def on_change():
    return AsyncMock()


@pytest.fixture
def listener(on_change):
    return IdleListener(
        host="127.0.0.1",
        port=1143,
        username="user",
        password="pass",
        starttls=True,
        cert_path="",
        on_change=on_change,
    )


def _mock_imapclient():
    """Create a mock IMAPClient for IDLE."""
    client = MagicMock()
    client.login = MagicMock()
    client.starttls = MagicMock()
    client.select_folder = MagicMock()
    client.idle = MagicMock()
    client.idle_check = MagicMock(return_value=[(2, b"EXISTS")])
    client.idle_done = MagicMock()
    client.logout = MagicMock()
    return client


class TestIdleListener:
    async def test_start_and_stop(self, listener):
        mock_client = _mock_imapclient()
        # Gate that blocks idle_check until we signal it
        gate = threading.Event()
        call_count = 0

        def mock_idle_check(timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                gate.wait(timeout=5)  # Blocks thread but releases on signal
            return [(2, b"EXISTS")]

        mock_client.idle_check = MagicMock(side_effect=mock_idle_check)
        with patch("email_mcp.idle.IMAPClient", return_value=mock_client):
            await listener.start()
            assert listener._task is not None
            # Let first iteration complete
            await asyncio.sleep(0)
            # Unblock the thread and stop
            gate.set()
            await listener.stop()
            assert listener._task is None

    async def test_calls_on_change_when_push_received(self, listener, on_change):
        mock_client = _mock_imapclient()
        gate = threading.Event()
        first_returned = asyncio.Event()
        call_count = 0

        def mock_idle_check(timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                gate.wait(timeout=5)
            return [(2, b"EXISTS")]

        mock_client.idle_check = MagicMock(side_effect=mock_idle_check)

        original_on_change = listener.on_change

        async def tracking_on_change():
            await original_on_change()
            first_returned.set()

        listener.on_change = tracking_on_change

        with patch("email_mcp.idle.IMAPClient", return_value=mock_client):
            await listener.start()
            # Wait for on_change to be called from first idle_check response
            await asyncio.wait_for(first_returned.wait(), timeout=2.0)
            gate.set()
            await listener.stop()

        on_change.assert_awaited()

    async def test_no_callback_on_empty_response(self, listener, on_change):
        mock_client = _mock_imapclient()
        gate = threading.Event()
        call_count = 0

        def mock_idle_check(timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                gate.wait(timeout=5)
            return []  # No changes

        mock_client.idle_check = MagicMock(side_effect=mock_idle_check)
        with patch("email_mcp.idle.IMAPClient", return_value=mock_client):
            await listener.start()
            # Let first iteration complete (no changes → no callback)
            await asyncio.sleep(0)
            gate.set()
            await listener.stop()

        on_change.assert_not_awaited()

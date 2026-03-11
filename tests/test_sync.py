"""Tests for email_mcp.sync — sync manager."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from email_mcp.sync import SyncError, SyncManager


@pytest.fixture
def sync_mgr():
    return SyncManager(
        mbsync_bin="mbsync",
        notmuch_bin="notmuch",
        timeout=10,
    )


def _mock_process(returncode=0, stdout=b"", stderr=b""):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = AsyncMock()
    return proc


async def test_sync_calls_mbsync_then_notmuch(sync_mgr):
    calls = []

    async def mock_exec(*cmd, **kwargs):
        calls.append(cmd)
        if "count" in cmd:
            return _mock_process(stdout=b"42\n")
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await sync_mgr.sync()

    assert len(calls) == 3
    assert calls[0][0] == "mbsync"
    assert calls[1][0] == "notmuch"
    assert "new" in calls[1]
    assert calls[2][0] == "notmuch"
    assert "count" in calls[2]


async def test_sync_updates_status(sync_mgr):
    async def mock_exec(*cmd, **kwargs):
        if "count" in cmd:
            return _mock_process(stdout=b"42\n")
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await sync_mgr.sync()

    assert sync_mgr.status.state == "ready"
    assert sync_mgr.status.last_sync is not None
    assert sync_mgr.status.last_index is not None
    assert sync_mgr.status.message_count == 42


async def test_sync_error_sets_error_status(sync_mgr):
    async def mock_exec(*cmd, **kwargs):
        return _mock_process(returncode=1, stderr=b"Connection refused")

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        with pytest.raises(SyncError):
            await sync_mgr.sync()

    assert sync_mgr.status.state == "error"
    assert "Connection refused" in sync_mgr.status.error


async def test_sync_serialized(sync_mgr):
    """Concurrent sync calls are serialized via lock."""
    call_count = 0

    async def mock_exec(*cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        if "count" in cmd:
            return _mock_process(stdout=b"10\n")
        await asyncio.sleep(0.01)
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await asyncio.gather(sync_mgr.sync(), sync_mgr.sync())

    # Each sync = 3 calls (mbsync, notmuch new, notmuch count)
    assert call_count == 6


async def test_sync_with_config(sync_mgr):
    sync_mgr.mbsync_config = "/path/to/.mbsyncrc"
    calls = []

    async def mock_exec(*cmd, **kwargs):
        calls.append(cmd)
        if "count" in cmd:
            return _mock_process(stdout=b"0\n")
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await sync_mgr.sync()

    mbsync_cmd = calls[0]
    assert "-c" in mbsync_cmd
    assert "/path/to/.mbsyncrc" in mbsync_cmd


async def test_initial_status():
    mgr = SyncManager()
    assert mgr.status.state == "initializing"
    assert mgr.status.last_sync is None

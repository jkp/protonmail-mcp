"""Tests for email_mcp.sync — sync engine."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from email_mcp.sync import SyncEngine, SyncError


@pytest.fixture
def engine():
    return SyncEngine(
        mbsync_bin="mbsync",
        notmuch_bin="notmuch",
        mbsync_channel="protonmail",
        timeout=10,
    )


def _mock_process(returncode=0, stdout=b"", stderr=b""):
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = AsyncMock()
    return proc


# -- Basic sync (backward compat) --


async def test_sync_calls_mbsync_then_notmuch(engine):
    calls = []

    async def mock_exec(*cmd, **kwargs):
        calls.append(cmd)
        if "count" in cmd:
            return _mock_process(stdout=b"42\n")
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await engine.sync()

    assert len(calls) == 3
    assert calls[0][0] == "mbsync"
    assert calls[1][0] == "notmuch"
    assert "new" in calls[1]
    assert calls[2][0] == "notmuch"
    assert "count" in calls[2]


async def test_sync_updates_status(engine):
    async def mock_exec(*cmd, **kwargs):
        if "count" in cmd:
            return _mock_process(stdout=b"42\n")
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await engine.sync()

    assert engine.status.state == "ready"
    assert engine.status.last_sync is not None
    assert engine.status.last_index is not None
    assert engine.status.message_count == 42


async def test_sync_error_sets_error_status(engine):
    async def mock_exec(*cmd, **kwargs):
        return _mock_process(returncode=1, stderr=b"Connection refused")

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        with pytest.raises(SyncError):
            await engine.sync()

    assert engine.status.state == "error"
    assert "Connection refused" in engine.status.error


async def test_sync_serialized(engine):
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
        await asyncio.gather(engine.sync(), engine.sync())

    # Each sync = 3 calls (mbsync, notmuch new, notmuch count)
    assert call_count == 6


async def test_sync_with_config(engine):
    engine.mbsync_config = "/path/to/.mbsyncrc"
    calls = []

    async def mock_exec(*cmd, **kwargs):
        calls.append(cmd)
        if "count" in cmd:
            return _mock_process(stdout=b"0\n")
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await engine.sync()

    mbsync_cmd = calls[0]
    assert "-c" in mbsync_cmd
    assert "/path/to/.mbsyncrc" in mbsync_cmd


async def test_initial_status():
    mgr = SyncEngine()
    assert mgr.status.state == "initializing"
    assert mgr.status.last_sync is None


# -- Per-folder sync --


async def test_sync_inbox_uses_channel_pattern(engine):
    calls = []

    async def mock_exec(*cmd, **kwargs):
        calls.append(cmd)
        if "count" in cmd:
            return _mock_process(stdout=b"100\n")
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await engine.sync_inbox()

    # Should call mbsync protonmail:INBOX
    mbsync_cmd = calls[0]
    assert mbsync_cmd[0] == "mbsync"
    assert "protonmail:INBOX" in mbsync_cmd


async def test_sync_all_uses_channel(engine):
    calls = []

    async def mock_exec(*cmd, **kwargs):
        calls.append(cmd)
        if "count" in cmd:
            return _mock_process(stdout=b"80000\n")
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        await engine.sync_all()

    mbsync_cmd = calls[0]
    assert mbsync_cmd[0] == "mbsync"
    assert "protonmail" in mbsync_cmd


# -- Singleton pattern --


async def test_request_sync_coalesces(engine):
    """When a sync is running, additional requests are coalesced."""
    call_count = 0

    async def mock_exec(*cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        if "count" in cmd:
            return _mock_process(stdout=b"10\n")
        # Simulate slow sync
        await asyncio.sleep(0.05)
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        # Fire multiple requests concurrently
        await asyncio.gather(
            engine.request_sync(),
            engine.request_sync(),
            engine.request_sync(),
        )

    # Should have done at most 2 syncs (1 running + 1 re-run for dirty)
    # Each sync = 3 calls (mbsync, notmuch new, notmuch count)
    assert call_count <= 6


# -- Debounced reindex --


async def test_request_reindex_debounces(engine):
    """Reindex requests are debounced."""
    engine.reindex_debounce = 0.1  # 100ms for testing
    calls = []

    async def mock_exec(*cmd, **kwargs):
        calls.append(cmd)
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        # Fire multiple reindex requests rapidly
        engine.request_reindex()
        engine.request_reindex()
        engine.request_reindex()
        # Wait for debounce to fire
        await asyncio.sleep(0.3)

    # Should have called notmuch new only once
    notmuch_new_calls = [c for c in calls if "new" in c]
    assert len(notmuch_new_calls) == 1


async def test_request_reindex_resets_timer(engine):
    """Later reindex requests reset the debounce timer."""
    engine.reindex_debounce = 0.2
    calls = []

    async def mock_exec(*cmd, **kwargs):
        calls.append(cmd)
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        engine.request_reindex()
        await asyncio.sleep(0.1)
        engine.request_reindex()  # Reset the timer
        await asyncio.sleep(0.1)
        # Timer hasn't fired yet since we reset it
        notmuch_calls_early = [c for c in calls if "new" in c]
        assert len(notmuch_calls_early) == 0
        # Wait for it to fire
        await asyncio.sleep(0.2)

    notmuch_calls = [c for c in calls if "new" in c]
    assert len(notmuch_calls) == 1


# -- Inbox periodic loop --


async def test_inbox_loop_calls_sync_inbox(engine):
    call_count = 0

    async def mock_exec(*cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        if "count" in cmd:
            return _mock_process(stdout=b"10\n")
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        engine.start_inbox_loop(interval=0.1)
        await asyncio.sleep(0.35)
        await engine.stop()

    # Should have run at least 2 inbox syncs
    assert call_count >= 6  # 2 syncs × 3 calls each


# -- Nightly scheduler --


async def test_nightly_calculates_next_run(engine):
    """Verify nightly scheduler creates a task."""
    async def mock_exec(*cmd, **kwargs):
        if "count" in cmd:
            return _mock_process(stdout=b"10\n")
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        engine.schedule_nightly(hour=3)
        assert engine._nightly_task is not None
        await engine.stop()


# -- Stop --


async def test_stop_cancels_tasks(engine):
    async def mock_exec(*cmd, **kwargs):
        if "count" in cmd:
            return _mock_process(stdout=b"10\n")
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        engine.start_inbox_loop(interval=1)
        engine.schedule_nightly(hour=3)
        await engine.stop()

    assert engine._inbox_task is None
    assert engine._nightly_task is None

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


def _mock_exec_factory(calls=None):
    """Create a mock exec that optionally records calls."""

    async def mock_exec(*cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        if "count" in cmd:
            return _mock_process(stdout=b"10\n")
        return _mock_process()

    return mock_exec


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
    sync_started = asyncio.Event()

    async def mock_exec(*cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        if "count" in cmd:
            return _mock_process(stdout=b"10\n")
        # Signal that sync has started, then yield to let other requests arrive
        sync_started.set()
        await asyncio.sleep(0)
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        t1 = asyncio.create_task(engine.request_sync())
        await sync_started.wait()
        t2 = asyncio.create_task(engine.request_sync())
        t3 = asyncio.create_task(engine.request_sync())
        await asyncio.gather(t1, t2, t3)

    # Should have done at most 2 syncs (1 running + 1 re-run for dirty)
    # Each sync = 3 calls (mbsync, notmuch new, notmuch count)
    assert call_count <= 6


# -- Debounced reindex --


async def test_request_reindex_debounces(engine):
    """Reindex requests are debounced — multiple rapid calls produce one execution."""
    engine.reindex_debounce = 0  # Fire on next event loop iteration
    reindex_done = asyncio.Event()
    calls = []

    async def mock_exec(*cmd, **kwargs):
        calls.append(cmd)
        return _mock_process()

    original_do_reindex = engine._do_reindex

    async def _tracked_reindex():
        await original_do_reindex()
        reindex_done.set()

    with (
        patch("asyncio.create_subprocess_exec", side_effect=mock_exec),
        patch.object(engine, "_do_reindex", side_effect=_tracked_reindex),
    ):
        engine.request_reindex()
        engine.request_reindex()
        engine.request_reindex()
        # Wait for the debounced reindex to complete
        await asyncio.wait_for(reindex_done.wait(), timeout=1.0)

    notmuch_new_calls = [c for c in calls if "new" in c]
    assert len(notmuch_new_calls) == 1


async def test_request_reindex_resets_timer(engine):
    """Later reindex requests cancel the previous timer and schedule a new one."""
    engine.reindex_debounce = 999  # Large value — should never actually fire
    calls = []

    async def mock_exec(*cmd, **kwargs):
        calls.append(cmd)
        return _mock_process()

    with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
        engine.request_reindex()
        first_handle = engine._reindex_handle
        assert first_handle is not None

        engine.request_reindex()
        second_handle = engine._reindex_handle
        assert second_handle is not None

        # First timer was cancelled, second is active
        assert first_handle.cancelled()
        assert not second_handle.cancelled()

        # Nothing fired (debounce too long)
        assert len(calls) == 0

    # Clean up
    engine._reindex_handle.cancel()
    engine._reindex_handle = None


# -- Inbox periodic loop --


async def test_inbox_loop_calls_sync_inbox(engine):
    """Periodic inbox loop calls sync_inbox on each iteration."""
    sync_count = 0
    target_syncs = 3

    original_sync_inbox = engine.sync_inbox

    async def counting_sync_inbox():
        nonlocal sync_count
        await original_sync_inbox()
        sync_count += 1
        if sync_count >= target_syncs:
            engine._inbox_task.cancel()

    async def noop_sleep(_seconds):
        await asyncio.sleep(0)  # Yield to event loop so cancellation can be delivered

    with (
        patch("asyncio.create_subprocess_exec", side_effect=_mock_exec_factory()),
        patch.object(engine, "sync_inbox", side_effect=counting_sync_inbox),
        patch.object(engine, "_sleep", side_effect=noop_sleep),
    ):
        engine.start_inbox_loop(interval=60)
        try:
            await engine._inbox_task
        except asyncio.CancelledError:
            pass

    assert sync_count == target_syncs


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

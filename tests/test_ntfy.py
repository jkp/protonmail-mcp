"""Tests for NTFY push notification processor."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from email_mcp.ntfy import (
    NOTIFICATION_RULES,
    NotifyRule,
    NtfyNotifier,
    NtfyProcessor,
)


@pytest.fixture
def notifier():
    return NtfyNotifier(url="https://ntfy.sh/test-topic")


@pytest.fixture
def processor(notifier):
    return NtfyProcessor(notifier)


class TestNtfyNotifier:
    @patch("email_mcp.ntfy.urlopen")
    async def test_send_posts_to_url(self, mock_urlopen, notifier):
        mock_urlopen.return_value = MagicMock()
        await notifier.send(title="Test", message="Something broke", priority="high", tags="warning")
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://ntfy.sh/test-topic"
        assert req.get_header("X-title") == "Test"
        assert req.get_header("X-priority") == "high"
        assert req.get_header("X-tags") == "warning"
        assert req.data == b"Something broke"

    @patch("email_mcp.ntfy.urlopen")
    async def test_send_with_separate_topic(self, mock_urlopen):
        mock_urlopen.return_value = MagicMock()
        n = NtfyNotifier(url="https://ntfy.sh", topic="my-alerts")
        await n.send(title="T", message="M")
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://ntfy.sh/my-alerts"

    @patch("email_mcp.ntfy.urlopen")
    async def test_send_swallows_exceptions(self, mock_urlopen, notifier):
        mock_urlopen.side_effect = Exception("network error")
        # Should not raise
        await notifier.send(title="T", message="M")

    @patch("email_mcp.ntfy.urlopen")
    async def test_send_default_priority(self, mock_urlopen, notifier):
        mock_urlopen.return_value = MagicMock()
        await notifier.send(title="T", message="M")
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("X-priority") == "default"


class TestNtfyProcessor:
    def test_matching_event_triggers_notification(self, processor):
        with patch.object(processor, "_schedule_send") as mock_send:
            event_dict = {"event": "server.imap_connect_failed", "level": "warning"}
            result = processor(None, "warning", event_dict)
            assert result is event_dict
            mock_send.assert_called_once()

    def test_non_matching_event_passes_through(self, processor):
        with patch.object(processor, "_schedule_send") as mock_send:
            event_dict = {"event": "server.starting", "level": "info"}
            result = processor(None, "info", event_dict)
            assert result is event_dict
            mock_send.assert_not_called()

    def test_debounce_suppresses_repeat(self, processor):
        with patch.object(processor, "_schedule_send") as mock_send:
            event_dict = {"event": "sync.error", "level": "error", "error": "mbsync failed"}
            # First call — should send
            processor(None, "error", event_dict)
            assert mock_send.call_count == 1

            # Second call within debounce window — should suppress
            processor(None, "error", event_dict)
            assert mock_send.call_count == 1

    def test_debounce_allows_after_window(self, processor):
        with patch.object(processor, "_schedule_send") as mock_send:
            event_dict = {"event": "sync.error", "level": "error", "error": "mbsync failed"}
            # First call
            processor(None, "error", event_dict)
            assert mock_send.call_count == 1

            # Fake the last_sent time to be in the past
            processor._last_sent["sync.error"] = time.monotonic() - 700
            processor(None, "error", event_dict)
            assert mock_send.call_count == 2

    def test_idle_error_suppressed_when_backoff_low(self, processor):
        with patch.object(processor, "_schedule_send") as mock_send:
            event_dict = {"event": "idle.error", "level": "warning", "backoff": 2}
            processor(None, "warning", event_dict)
            mock_send.assert_not_called()

    def test_idle_error_triggers_when_backoff_high(self, processor):
        with patch.object(processor, "_schedule_send") as mock_send:
            event_dict = {"event": "idle.error", "level": "warning", "backoff": 60}
            processor(None, "warning", event_dict)
            mock_send.assert_called_once()

    def test_startup_events_have_zero_debounce(self, processor):
        with patch.object(processor, "_schedule_send") as mock_send:
            event_dict = {"event": "server.imap_connect_failed", "level": "warning"}
            processor(None, "warning", event_dict)
            processor(None, "warning", event_dict)
            # Zero debounce — both should send
            assert mock_send.call_count == 2

    def test_message_formatting_includes_error(self, processor):
        with patch.object(processor, "_schedule_send") as mock_send:
            event_dict = {
                "event": "sync.error",
                "level": "error",
                "error": "mbsync failed: connection refused",
                "elapsed_s": 5.2,
            }
            processor(None, "error", event_dict)
            call_kwargs = mock_send.call_args
            title = call_kwargs[0][0]
            message = call_kwargs[0][1]
            assert "Sync Error" in title
            assert "mbsync failed: connection refused" in message

    def test_message_formatting_includes_message_id(self, processor):
        with patch.object(processor, "_schedule_send") as mock_send:
            event_dict = {
                "event": "tool.archive.imap_failed",
                "level": "error",
                "message_id": "abc123@example.com",
                "error": "Message not found",
            }
            processor(None, "error", event_dict)
            message = mock_send.call_args[0][1]
            assert "abc123@example.com" in message


class TestNotificationRules:
    def test_all_rules_have_valid_priorities(self):
        valid = {"urgent", "high", "default", "low", "min"}
        for event, rule in NOTIFICATION_RULES.items():
            assert rule.priority in valid, f"{event} has invalid priority {rule.priority}"

    def test_startup_events_have_zero_debounce(self):
        startup_events = [
            "server.imap_connect_failed",
            "server.full_sync_on_startup.failed",
            "server.startup_sync.failed",
            "server.idle_start_failed",
        ]
        for event in startup_events:
            assert event in NOTIFICATION_RULES
            assert NOTIFICATION_RULES[event].debounce_s == 0

    def test_idle_error_has_min_backoff(self):
        rule = NOTIFICATION_RULES["idle.error"]
        assert rule.min_backoff > 0


class TestIntegration:
    @patch("email_mcp.ntfy.urlopen")
    async def test_end_to_end_notification(self, mock_urlopen):
        mock_urlopen.return_value = MagicMock()
        notifier = NtfyNotifier(url="https://ntfy.sh/test")
        processor = NtfyProcessor(notifier)

        event_dict = {"event": "server.imap_connect_failed", "level": "warning"}
        processor(None, "warning", event_dict)

        # Allow background task to complete
        await asyncio.sleep(0.1)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("X-priority") == "urgent"

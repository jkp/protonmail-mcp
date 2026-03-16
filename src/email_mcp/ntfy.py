"""NTFY push notifications via structlog processor."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.request import Request, urlopen

import structlog

logger = structlog.get_logger()


@dataclass
class NotifyRule:
    """Configuration for a notification trigger."""

    priority: str = "default"
    debounce_s: int = 0
    tags: str = ""
    min_backoff: int = 0


NOTIFICATION_RULES: dict[str, NotifyRule] = {
    # Infrastructure failures — always notify, urgent priority
    "server.imap_connect_failed": NotifyRule(priority="urgent", debounce_s=0, tags="rotating_light"),
    "server.full_sync_on_startup.failed": NotifyRule(priority="urgent", debounce_s=0, tags="rotating_light"),
    "server.startup_sync.failed": NotifyRule(priority="urgent", debounce_s=0, tags="rotating_light"),
    "server.idle_start_failed": NotifyRule(priority="urgent", debounce_s=0, tags="rotating_light"),
    # Sync failures — debounce heavily (these retry every 60s)
    "sync.timeout": NotifyRule(priority="high", debounce_s=600, tags="warning"),
    "sync.error": NotifyRule(priority="high", debounce_s=600, tags="warning"),
    "sync.reindex_failed": NotifyRule(priority="high", debounce_s=600, tags="warning"),
    # IDLE degradation — only notify when backoff is significant
    "idle.error": NotifyRule(priority="high", debounce_s=300, tags="warning", min_backoff=60),
    # IMAP mutation failures — per-failure, moderate debounce
    "tool.archive.imap_failed": NotifyRule(priority="default", debounce_s=60, tags="email"),
    "tool.delete.imap_failed": NotifyRule(priority="default", debounce_s=60, tags="email"),
    "tool.move_email.imap_failed": NotifyRule(priority="default", debounce_s=60, tags="email"),
}

# Keys to extract from event_dict for notification body
_CONTEXT_KEYS = ("error", "stderr", "message_id", "elapsed_s", "backoff", "detail")


def _format_title(event: str) -> str:
    """Convert event name to human-readable title."""
    # "sync.error" -> "Sync Error", "tool.archive.imap_failed" -> "Tool Archive Imap Failed"
    return event.replace(".", " ").replace("_", " ").title()


def _format_body(event_dict: dict[str, Any]) -> str:
    """Extract useful context from event_dict into a notification body."""
    lines = []
    for key in _CONTEXT_KEYS:
        if key in event_dict:
            lines.append(f"{key}: {event_dict[key]}")
    return "\n".join(lines) if lines else event_dict.get("event", "")


class NtfyNotifier:
    """Fire-and-forget HTTP POST to an NTFY server."""

    def __init__(self, url: str, topic: str = "") -> None:
        self.url = f"{url.rstrip('/')}/{topic}" if topic else url

    async def send(
        self,
        title: str,
        message: str,
        priority: str = "default",
        tags: str = "",
    ) -> None:
        try:
            await asyncio.to_thread(self._send_sync, title, message, priority, tags)
        except Exception:
            pass  # Notification failures must never crash the server

    def _send_sync(self, title: str, message: str, priority: str, tags: str) -> None:
        req = Request(self.url, data=message.encode())
        req.add_header("X-Title", title)
        req.add_header("X-Priority", priority)
        if tags:
            req.add_header("X-Tags", tags)
        urlopen(req, timeout=10)


@dataclass
class NtfyProcessor:
    """Structlog processor that fires NTFY notifications for matching events."""

    notifier: NtfyNotifier
    rules: dict[str, NotifyRule] = field(default_factory=lambda: NOTIFICATION_RULES)
    _last_sent: dict[str, float] = field(default_factory=dict)

    def __call__(
        self, logger: Any, method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        event = event_dict.get("event", "")
        rule = self.rules.get(event)
        if rule is None:
            return event_dict

        # Check min_backoff filter (for idle.error)
        if rule.min_backoff > 0:
            backoff = event_dict.get("backoff", 0)
            if backoff < rule.min_backoff:
                return event_dict

        # Check debounce
        now = time.monotonic()
        if rule.debounce_s > 0:
            last = self._last_sent.get(event, 0)
            if now - last < rule.debounce_s:
                return event_dict

        self._last_sent[event] = now

        title = _format_title(event)
        message = _format_body(event_dict)
        self._schedule_send(title, message, rule)

        return event_dict

    def _schedule_send(self, title: str, message: str, rule: NotifyRule) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self.notifier.send(
                    title=title,
                    message=message,
                    priority=rule.priority,
                    tags=rule.tags,
                )
            )
        except RuntimeError:
            # No running event loop (e.g. during testing without async)
            pass

"""Structured logging configuration using structlog."""

import logging
import sys

import structlog


def configure_logging(
    level: str = "INFO", ntfy_url: str = "", ntfy_topic: str = ""
) -> None:
    """Configure structlog with JSON rendering for production use."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=numeric_level,
        force=True,
    )

    processors: list = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if ntfy_url:
        from email_mcp.ntfy import NtfyNotifier, NtfyProcessor

        notifier = NtfyNotifier(url=ntfy_url, topic=ntfy_topic)
        processors.append(NtfyProcessor(notifier))

    processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

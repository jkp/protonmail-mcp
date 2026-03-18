"""Structured logging configuration using structlog."""

import logging
import sys
from pathlib import Path

import structlog


def configure_logging(
    level: str = "INFO", ntfy_url: str = "", ntfy_topic: str = "",
    log_file: Path | None = None,
) -> None:
    """Configure structlog with JSON rendering.

    When running interactively (TTY), routes JSON logs to a file so rich
    progress bars are not polluted. When running non-interactively (MCP stdio),
    logs go to stderr as normal.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    interactive = sys.stderr.isatty()

    if interactive and log_file is not None:
        # Route ALL logs to file, leave stderr clean for rich progress bars
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(log_file))
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.root.handlers = [handler]
        logging.root.setLevel(numeric_level)
        # Also suppress httpx request logging in interactive mode
        logging.getLogger("httpx").setLevel(logging.WARNING)
    else:
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

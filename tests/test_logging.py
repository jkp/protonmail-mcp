"""Tests for structured logging configuration."""

import json
import logging

import pytest
import structlog

from protonmail_mcp.logging import configure_logging


@pytest.fixture(autouse=True)
def _reset_structlog():
    """Reset structlog state between tests to avoid cached loggers."""
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


class TestConfigureLogging:
    def test_json_output(self, capsys) -> None:
        """Logging should produce JSON output to stderr."""
        configure_logging("INFO")
        logger = structlog.get_logger()
        logger.info("test.event", key="value")

        captured = capsys.readouterr()
        line = captured.err.strip().split("\n")[-1]
        parsed = json.loads(line)
        assert parsed["event"] == "test.event"
        assert parsed["key"] == "value"
        assert parsed["level"] == "info"
        assert "timestamp" in parsed

    def test_level_filtering(self, capsys) -> None:
        """Debug messages should be filtered at INFO level."""
        configure_logging("INFO")
        logger = structlog.get_logger()
        logger.debug("should.not.appear")
        logger.info("should.appear")

        captured = capsys.readouterr()
        assert "should.not.appear" not in captured.err
        assert "should.appear" in captured.err

    def test_debug_level(self, capsys) -> None:
        """Debug messages should appear at DEBUG level."""
        configure_logging("DEBUG")
        logger = structlog.get_logger()
        logger.debug("debug.visible")

        captured = capsys.readouterr()
        assert "debug.visible" in captured.err

    def test_invalid_level_defaults_to_info(self) -> None:
        """Invalid log level should default to INFO."""
        configure_logging("INVALID")
        root = logging.getLogger()
        assert root.level == logging.INFO

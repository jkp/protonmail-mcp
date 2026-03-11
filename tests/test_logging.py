"""Tests for email_mcp.logging."""

import logging

from email_mcp.logging import configure_logging


def test_configure_logging_sets_level():
    configure_logging("DEBUG")
    root = logging.getLogger()
    assert root.level == logging.DEBUG


def test_configure_logging_default():
    configure_logging()
    root = logging.getLogger()
    assert root.level == logging.INFO

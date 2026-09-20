"""Tests for server startup wiring.

These guard the lifespan contract specifically. FastMCP invokes the lifespan as
``async with self._lifespan(self)``, so it must be an ``@asynccontextmanager``.
A bare async generator looks correct, imports fine, and passes every other test
in the suite -- it only fails at server startup with

    TypeError: 'async_generator' object does not support the asynchronous
    context manager protocol

which is how it reached production and crash-looped the container.
"""

from __future__ import annotations

import inspect


def test_lifespan_is_an_async_context_manager() -> None:
    from email_mcp.server import _lifespan

    assert hasattr(_lifespan, "__wrapped__"), (
        "_lifespan must be wrapped by @asynccontextmanager -- FastMCP uses it as "
        "`async with self._lifespan(self)`"
    )
    assert inspect.isasyncgenfunction(_lifespan.__wrapped__), (
        "_lifespan's wrapped function should still be an async generator"
    )


def test_calling_lifespan_yields_a_context_manager() -> None:
    """The object FastMCP enters must support the async CM protocol."""
    from email_mcp.server import _lifespan

    # Not entered, so the generator body never runs and there is nothing to close.
    cm = _lifespan(None)
    assert hasattr(cm, "__aenter__") and hasattr(cm, "__aexit__")

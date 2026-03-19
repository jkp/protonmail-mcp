"""Tests for the lazy email summarizer."""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from email_mcp.db import Database, MessageRow


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def _insert_message(
    db: Database,
    pm_id: str,
    subject: str = "Test",
    sender_name: str = "Alice",
    sender_email: str = "alice@example.com",
    body: str | None = None,
) -> None:
    db.messages.upsert(
        MessageRow(
            pm_id=pm_id,
            message_id=f"{pm_id}@example.com",
            subject=subject,
            sender_name=sender_name,
            sender_email=sender_email,
            recipients=[],
            date=int(time.time()),
            unread=False,
            label_ids=["0"],
            folder="INBOX",
            size=1024,
            has_attachments=False,
            body_indexed=bool(body),
        )
    )
    if body:
        db.bodies.insert(pm_id, body)


class TestSummaryColumn:
    def test_summary_column_exists(self, db: Database) -> None:
        cols = {row[1] for row in db.execute("PRAGMA table_info(messages)").fetchall()}
        assert "summary" in cols

    def test_summary_null_by_default(self, db: Database) -> None:
        _insert_message(db, "pm-1", body="Hello world")
        row = db.execute("SELECT summary FROM messages WHERE pm_id = 'pm-1'").fetchone()
        assert row[0] is None

    def test_summary_stored_and_retrieved(self, db: Database) -> None:
        _insert_message(db, "pm-1", body="Hello world")
        db.execute(
            "UPDATE messages SET summary = ? WHERE pm_id = ?",
            ["A greeting email.", "pm-1"],
        )
        db.commit()
        row = db.execute("SELECT summary FROM messages WHERE pm_id = 'pm-1'").fetchone()
        assert row[0] == "A greeting email."


class TestSummarize:
    async def test_returns_cached_summary(self, db: Database) -> None:
        from email_mcp.summarizer import summarize_messages

        _insert_message(db, "pm-1", body="Hello world")
        db.execute(
            "UPDATE messages SET summary = ? WHERE pm_id = ?",
            ["Cached summary.", "pm-1"],
        )
        db.commit()

        results = await summarize_messages(["pm-1"], db, api_key="fake")
        assert results["pm-1"] == "Cached summary."

    async def test_calls_llm_for_missing_summary(self, db: Database) -> None:
        from email_mcp.summarizer import summarize_messages

        _insert_message(db, "pm-1", subject="Invoice", body="Please pay $100.")

        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Invoice requesting $100 payment."}}]
        }

        with patch("email_mcp.summarizer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            results = await summarize_messages(["pm-1"], db, api_key="test-key")

        assert results["pm-1"] == "Invoice requesting $100 payment."

    async def test_caches_summary_in_db(self, db: Database) -> None:
        from email_mcp.summarizer import summarize_messages

        _insert_message(db, "pm-1", subject="Meeting", body="Let's meet at 3pm.")

        with patch(
            "email_mcp.summarizer._llm_summarize",
            new_callable=AsyncMock,
            return_value="Meeting request for 3pm.",
        ):
            await summarize_messages(["pm-1"], db, api_key="test-key")

        row = db.execute("SELECT summary FROM messages WHERE pm_id = 'pm-1'").fetchone()
        assert row[0] == "Meeting request for 3pm."

    async def test_mixed_cached_and_uncached(self, db: Database) -> None:
        from email_mcp.summarizer import summarize_messages

        _insert_message(db, "pm-cached", body="Old email")
        db.execute(
            "UPDATE messages SET summary = ? WHERE pm_id = ?",
            ["Already summarized.", "pm-cached"],
        )
        db.commit()

        _insert_message(db, "pm-new", subject="New", body="Fresh email content.")

        with patch(
            "email_mcp.summarizer._llm_summarize",
            new_callable=AsyncMock,
            return_value="New email summary.",
        ) as mock_llm:
            results = await summarize_messages(
                ["pm-cached", "pm-new"], db, api_key="test-key"
            )

        assert results["pm-cached"] == "Already summarized."
        assert results["pm-new"] == "New email summary."
        assert mock_llm.call_count == 1

    async def test_no_api_key_returns_empty(self, db: Database) -> None:
        from email_mcp.summarizer import summarize_messages

        _insert_message(db, "pm-1", body="Hello")
        results = await summarize_messages(["pm-1"], db, api_key="")
        assert results == {}

    async def test_no_body_skips_summary(self, db: Database) -> None:
        from email_mcp.summarizer import summarize_messages

        _insert_message(db, "pm-1")  # no body
        results = await summarize_messages(["pm-1"], db, api_key="test-key")
        assert "pm-1" not in results

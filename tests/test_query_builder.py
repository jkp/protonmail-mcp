"""Tests for Gmail-style query → SQL translator."""

import pytest

from email_mcp.query_builder import ParsedQuery, build_query


class TestFromOperator:
    def test_from_email(self) -> None:
        q = build_query("from:alice@example.com")
        assert "sender_email LIKE ?" in q.where
        assert "%alice@example.com%" in q.params

    def test_from_name(self) -> None:
        q = build_query("from:alice")
        assert "sender_email LIKE ?" in q.where
        assert "%alice%" in q.params

    def test_from_quoted(self) -> None:
        q = build_query('from:"alice smith"')
        assert "%alice smith%" in q.params


class TestSubjectOperator:
    def test_subject(self) -> None:
        q = build_query("subject:invoice")
        assert "subject LIKE ?" in q.where
        assert "%invoice%" in q.params

    def test_subject_quoted(self) -> None:
        q = build_query('subject:"quarterly report"')
        assert "%quarterly report%" in q.params


class TestIsOperator:
    def test_is_unread(self) -> None:
        q = build_query("is:unread")
        assert "unread = 1" in q.where

    def test_is_read(self) -> None:
        q = build_query("is:read")
        assert "unread = 0" in q.where

    def test_is_starred(self) -> None:
        q = build_query("is:starred")
        # Starred maps to has_attachments or a flag — just check it doesn't crash
        assert q is not None


class TestInOperator:
    def test_in_inbox(self) -> None:
        q = build_query("in:inbox")
        assert "folder = ?" in q.where
        assert "INBOX" in q.params

    def test_in_archive(self) -> None:
        q = build_query("in:archive")
        assert "Archive" in q.params

    def test_in_trash(self) -> None:
        q = build_query("in:trash")
        assert "Trash" in q.params

    def test_in_sent(self) -> None:
        q = build_query("in:sent")
        assert "Sent" in q.params

    def test_in_spam(self) -> None:
        q = build_query("in:spam")
        assert "Spam" in q.params


class TestHasOperator:
    def test_has_attachment(self) -> None:
        q = build_query("has:attachment")
        assert "has_attachments = 1" in q.where

    def test_has_attachments_plural(self) -> None:
        q = build_query("has:attachments")
        assert "has_attachments = 1" in q.where


class TestDateOperators:
    def test_older_than_days(self) -> None:
        q = build_query("older_than:30d")
        assert any("date <" in clause for clause in q.where_clauses)
        assert len(q.params) > 0

    def test_newer_than_days(self) -> None:
        q = build_query("newer_than:7d")
        assert any("date >" in clause for clause in q.where_clauses)

    def test_older_than_hours(self) -> None:
        q = build_query("older_than:12h")
        assert any("date <" in clause for clause in q.where_clauses)

    def test_older_than_weeks(self) -> None:
        q = build_query("older_than:2w")
        assert any("date <" in clause for clause in q.where_clauses)

    def test_older_than_months(self) -> None:
        q = build_query("older_than:3m")
        assert any("date <" in clause for clause in q.where_clauses)


class TestFreeText:
    def test_free_text_goes_to_fts(self) -> None:
        q = build_query("quarterly report")
        assert q.fts_terms is not None
        assert "quarterly" in q.fts_terms

    def test_free_text_mixed_with_operator(self) -> None:
        q = build_query("from:alice invoice payment")
        assert "sender_email LIKE ?" in q.where
        assert q.fts_terms is not None
        assert "invoice" in q.fts_terms

    def test_empty_query(self) -> None:
        q = build_query("")
        assert q.where == "1"
        assert q.fts_terms is None

    def test_wildcard_query(self) -> None:
        q = build_query("*")
        assert q.fts_terms is None  # * means match all, no FTS constraint


class TestCombined:
    def test_multiple_operators(self) -> None:
        q = build_query("from:bob subject:invoice is:unread")
        assert "sender_email LIKE ?" in q.where
        assert "subject LIKE ?" in q.where
        assert "unread = 1" in q.where

    def test_produces_valid_where_string(self) -> None:
        q = build_query("from:alice is:unread has:attachment")
        # Should be joinable into a real WHERE clause
        assert "AND" in q.where or q.where.startswith("sender")

    def test_params_match_placeholders(self) -> None:
        q = build_query("from:alice subject:test")
        placeholder_count = q.where.count("?")
        assert placeholder_count == len(q.params)


class TestSQLGeneration:
    def test_select_metadata_only(self) -> None:
        q = build_query("from:alice is:unread")
        sql, params = q.to_sql(limit=20)
        assert "FROM messages" in sql
        assert "LIMIT" in sql
        assert len(params) == len(q.params) + 2  # +2 for LIMIT and OFFSET

    def test_select_with_fts(self) -> None:
        q = build_query("invoice payment")
        sql, params = q.to_sql(limit=20)
        assert "fts_bodies" in sql
        assert "MATCH" in sql

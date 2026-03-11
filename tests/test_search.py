"""Tests for email_mcp.search — query translation and notmuch wrapper."""

from email_mcp.search import translate_query


class TestQueryTranslation:
    def test_has_attachment(self):
        assert translate_query("has:attachment") == "tag:attachment"

    def test_is_unread(self):
        assert translate_query("is:unread") == "tag:unread"

    def test_is_starred(self):
        assert translate_query("is:starred") == "tag:flagged"

    def test_is_read_becomes_not_unread(self):
        result = translate_query("is:read")
        assert "NOT" in result
        assert "tag:unread" in result

    def test_in_inbox(self):
        assert translate_query("in:inbox") == "folder:INBOX"

    def test_in_sent(self):
        assert translate_query("in:sent") == "folder:Sent"

    def test_in_archive(self):
        assert translate_query("in:archive") == "folder:Archive"

    def test_label_becomes_tag(self):
        assert translate_query("label:important") == "tag:important"

    def test_filename_becomes_attachment(self):
        assert translate_query("filename:report.pdf") == "attachment:report.pdf"

    def test_newer_than(self):
        assert translate_query("newer_than:7d") == "date:7days.."

    def test_older_than(self):
        assert translate_query("older_than:30d") == "date:..30days"

    def test_passthrough_notmuch_native(self):
        assert translate_query("from:alice") == "from:alice"
        assert translate_query("subject:hello") == "subject:hello"

    def test_combined_query(self):
        result = translate_query("from:alice in:inbox")
        assert "from:alice" in result
        assert "folder:INBOX" in result

    def test_case_insensitive_folder(self):
        assert translate_query("in:INBOX") == "folder:INBOX"
        assert translate_query("in:Inbox") == "folder:INBOX"

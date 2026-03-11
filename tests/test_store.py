"""Tests for email_mcp.store — Maildir operations."""



from email_mcp.store import (
    FLAG_FLAGGED,
    FLAG_SEEN,
    MaildirStore,
    _get_flags,
    _parse_address,
    _parse_address_list,
    _set_flags,
)


class TestParseAddress:
    def test_simple_address(self):
        addr = _parse_address("alice@example.com")
        assert addr.addr == "alice@example.com"

    def test_named_address(self):
        addr = _parse_address("Alice Smith <alice@example.com>")
        assert addr.name == "Alice Smith"
        assert addr.addr == "alice@example.com"

    def test_empty(self):
        addr = _parse_address("")
        assert addr.addr == ""


class TestParseAddressList:
    def test_multiple(self):
        addrs = _parse_address_list("alice@example.com, bob@example.com")
        assert len(addrs) == 2
        assert addrs[0].addr == "alice@example.com"
        assert addrs[1].addr == "bob@example.com"

    def test_empty(self):
        assert _parse_address_list(None) == []
        assert _parse_address_list("") == []


class TestFlags:
    def test_get_flags_seen(self, tmp_path):
        f = tmp_path / "msg:2,S"
        f.touch()
        assert _get_flags(f) == "S"

    def test_get_flags_multiple(self, tmp_path):
        f = tmp_path / "msg:2,FRS"
        f.touch()
        assert _get_flags(f) == "FRS"

    def test_get_flags_none(self, tmp_path):
        f = tmp_path / "msg"
        f.touch()
        assert _get_flags(f) == ""

    def test_set_flags(self, tmp_path):
        f = tmp_path / "msg:2,S"
        f.touch()
        new_path = _set_flags(f, "FR")
        assert new_path.name == "msg:2,FR"
        assert new_path.exists()
        assert not f.exists()

    def test_set_flags_adds_suffix(self, tmp_path):
        f = tmp_path / "msg"
        f.touch()
        new_path = _set_flags(f, "S")
        assert new_path.name == "msg:2,S"

    def test_set_flags_sorts(self, tmp_path):
        f = tmp_path / "msg:2,"
        f.touch()
        new_path = _set_flags(f, "SRF")
        assert new_path.name == "msg:2,FRS"


class TestListFolders:
    def test_lists_all_folders(self, store):
        folders = store.list_folders()
        names = {f.name for f in folders}
        assert "INBOX" in names
        assert "Sent" in names
        assert "Archive" in names
        assert "Trash" in names

    def test_folder_counts(self, store):
        folders = store.list_folders()
        inbox = next(f for f in folders if f.name == "INBOX")
        assert inbox.count == 4

    def test_empty_maildir(self, tmp_path):
        s = MaildirStore(tmp_path / "nonexistent")
        assert s.list_folders() == []


class TestListEmails:
    def test_lists_inbox(self, store):
        emails = store.list_emails("INBOX")
        assert len(emails) == 4

    def test_respects_limit(self, store):
        emails = store.list_emails("INBOX", limit=2)
        assert len(emails) == 2

    def test_respects_offset(self, store):
        all_emails = store.list_emails("INBOX")
        offset_emails = store.list_emails("INBOX", offset=2)
        assert len(offset_emails) == 2
        assert offset_emails[0].message_id == all_emails[2].message_id

    def test_has_message_id(self, store):
        emails = store.list_emails("INBOX")
        assert all(e.message_id for e in emails)

    def test_has_subject(self, store):
        emails = store.list_emails("INBOX")
        subjects = {e.subject for e in emails}
        assert "Hello Bob" in subjects
        assert "Meeting Tomorrow" in subjects

    def test_has_from(self, store):
        emails = store.list_emails("INBOX")
        from_addrs = {e.from_.addr for e in emails}
        assert "alice@example.com" in from_addrs

    def test_has_flags(self, store):
        emails = store.list_emails("INBOX")
        seen_emails = [e for e in emails if FLAG_SEEN in e.flags]
        assert len(seen_emails) == 3  # msg1, msg3, msg4 are Seen

    def test_empty_folder(self, store):
        emails = store.list_emails("Archive")
        assert emails == []


class TestReadEmail:
    def test_reads_plain_text(self, store):
        email = store.read_email("<msg1@example.com>")
        assert email is not None
        assert email.subject == "Hello Bob"
        assert "Hey there!" in email.body_plain
        assert email.from_.name == "Alice Smith"
        assert email.from_.addr == "alice@example.com"

    def test_reads_html_email(self, store):
        email = store.read_email("<msg3@example.com>")
        assert email is not None
        assert email.subject == "Weekly Update"
        assert "Weekly Update" in email.body_html
        assert "HTML version" in email.body_html

    def test_reads_cc(self, store):
        email = store.read_email("<msg2@example.com>")
        assert email is not None
        assert len(email.cc) == 1
        assert email.cc[0].addr == "alice@example.com"

    def test_reads_attachments(self, store):
        email = store.read_email("<msg4@example.com>")
        assert email is not None
        assert len(email.attachments) == 1
        assert email.attachments[0].filename == "report.txt"

    def test_not_found(self, store):
        assert store.read_email("<nonexistent@example.com>") is None

    def test_folder_hint(self, store):
        email = store.read_email("<msg1@example.com>", folder="INBOX")
        assert email is not None
        assert email.folder == "INBOX"

    def test_wrong_folder_hint(self, store):
        email = store.read_email("<msg1@example.com>", folder="Sent")
        assert email is None

    def test_has_date(self, store):
        email = store.read_email("<msg1@example.com>")
        assert email is not None
        assert email.date is not None
        assert email.date_str != ""


class TestMoveEmail:
    def test_moves_to_archive(self, store):
        assert store.archive_email("<msg1@example.com>")
        # Should not be in INBOX anymore
        assert store.read_email("<msg1@example.com>", folder="INBOX") is None
        # Should be in Archive
        assert store.read_email("<msg1@example.com>", folder="Archive") is not None

    def test_moves_to_trash(self, store):
        assert store.delete_email("<msg1@example.com>")
        assert store.read_email("<msg1@example.com>", folder="INBOX") is None
        assert store.read_email("<msg1@example.com>", folder="Trash") is not None

    def test_move_between_folders(self, store):
        assert store.move_email("<msg1@example.com>", "Sent", "INBOX")
        assert store.read_email("<msg1@example.com>", folder="INBOX") is None
        assert store.read_email("<msg1@example.com>", folder="Sent") is not None

    def test_move_nonexistent(self, store):
        assert not store.move_email("<nonexistent@example.com>", "Archive")

    def test_creates_dest_folder(self, store, maildir):
        assert store.move_email("<msg1@example.com>", "NewFolder")
        assert (maildir / "NewFolder" / "cur").is_dir()


class TestFlagOperations:
    def test_add_flag(self, store):
        assert store.add_flag("<msg2@example.com>", FLAG_FLAGGED)
        email = store.read_email("<msg2@example.com>")
        assert email is not None
        assert FLAG_FLAGGED in email.flags

    def test_remove_flag(self, store):
        assert store.remove_flag("<msg1@example.com>", FLAG_SEEN)
        email = store.read_email("<msg1@example.com>")
        assert email is not None
        assert FLAG_SEEN not in email.flags

    def test_set_flags(self, store):
        assert store.set_flags("<msg1@example.com>", "FR")
        email = store.read_email("<msg1@example.com>")
        assert email is not None
        assert email.flags == "FR"

    def test_flag_nonexistent(self, store):
        assert not store.add_flag("<nonexistent@example.com>", FLAG_SEEN)


class TestGetAttachmentContent:
    def test_gets_content(self, store):
        result = store.get_attachment_content("<msg4@example.com>", "report.txt")
        assert result is not None
        content, content_type = result
        assert content == b"Report content here"
        assert content_type == "text/plain"

    def test_nonexistent_attachment(self, store):
        result = store.get_attachment_content("<msg4@example.com>", "nonexistent.txt")
        assert result is None

    def test_nonexistent_email(self, store):
        result = store.get_attachment_content("<nonexistent@example.com>", "report.txt")
        assert result is None


class TestSaveMessage:
    def test_saves_to_folder(self, store, maildir):
        raw = b"From: test@example.com\r\nTo: bob@example.com\r\nSubject: Saved\r\n\r\nBody"
        path = store.save_message("Sent", raw)
        assert path.exists()
        assert path.parent == maildir / "Sent" / "cur"
        assert path.read_bytes() == raw

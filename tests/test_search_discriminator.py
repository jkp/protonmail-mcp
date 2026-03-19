"""Tests for the promotional email discriminator in search ranking."""

import time
from pathlib import Path

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
    folder: str = "INBOX",
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
            folder=folder,
            size=1024,
            has_attachments=False,
            body_indexed=bool(body),
        )
    )
    if body:
        db.bodies.insert(pm_id, body)
        db.messages.mark_body_indexed(pm_id)


class TestIsPromotional:
    """Detect promotional emails by unsubscribe markers in body."""

    def test_english_unsubscribe(self):
        from email_mcp.tools.searching import is_promotional

        body = "Great deals! Click here to unsubscribe from this mailing list."
        assert is_promotional(body) is True

    def test_swedish_avregistrera(self):
        from email_mcp.tools.searching import is_promotional

        body = "Fantastiska erbjudanden! Klicka här för att avregistrera dig."
        assert is_promotional(body) is True

    def test_swedish_prenumeration(self):
        from email_mcp.tools.searching import is_promotional

        body = "Hantera din prenumeration genom att klicka nedan."
        assert is_promotional(body) is True

    def test_normal_email_not_promotional(self):
        from email_mcp.tools.searching import is_promotional

        body = "Hi Jamie, your physio appointment is confirmed for Thursday at 2pm."
        assert is_promotional(body) is False

    def test_case_insensitive(self):
        from email_mcp.tools.searching import is_promotional

        body = "Click here to UNSUBSCRIBE from our list."
        assert is_promotional(body) is True

    def test_empty_body(self):
        from email_mcp.tools.searching import is_promotional

        assert is_promotional("") is False

    def test_list_unsubscribe_variant(self):
        """Emails with list-unsubscribe headers often have the word in body too."""
        from email_mcp.tools.searching import is_promotional

        body = "To unsubscribe or manage preferences, visit our website."
        assert is_promotional(body) is True


class TestWantsPromos:
    """Detect when the user is explicitly searching for promotional content."""

    def test_newsletter_query(self):
        from email_mcp.tools.searching import wants_promos

        assert wants_promos("newsletter") is True

    def test_promo_query(self):
        from email_mcp.tools.searching import wants_promos

        assert wants_promos("promotional offers") is True

    def test_subscription_query(self):
        from email_mcp.tools.searching import wants_promos

        assert wants_promos("subscription emails") is True

    def test_unsubscribe_query(self):
        from email_mcp.tools.searching import wants_promos

        assert wants_promos("how to unsubscribe") is True

    def test_marketing_query(self):
        from email_mcp.tools.searching import wants_promos

        assert wants_promos("marketing emails") is True

    def test_normal_query(self):
        from email_mcp.tools.searching import wants_promos

        assert wants_promos("physio appointment") is False

    def test_normal_query_with_common_words(self):
        from email_mcp.tools.searching import wants_promos

        assert wants_promos("benson headphones") is False


class TestCheckBulk:
    """Layered bulk detection: headers > newsletter_id > body text fallback."""

    def test_detects_via_newsletter_id(self, db):
        from email_mcp.tools.searching import check_bulk

        _insert_message(db, "pm-nl", body="Some content")
        db.execute("UPDATE messages SET newsletter_id = 'nl-123' WHERE pm_id = 'pm-nl'")
        db.commit()

        assert check_bulk(["pm-nl"], db) == {"pm-nl"}

    def test_detects_via_list_unsubscribe_header(self, db):
        import json

        from email_mcp.tools.searching import check_bulk

        _insert_message(db, "pm-hdr", body="No unsub text here")
        db.execute(
            "UPDATE messages SET parsed_headers = ?, headers_indexed = 1 WHERE pm_id = ?",
            [json.dumps({"List-Unsubscribe": "<mailto:unsub@ex.com>"}), "pm-hdr"],
        )
        db.commit()

        assert check_bulk(["pm-hdr"], db) == {"pm-hdr"}

    def test_falls_back_to_body_text(self, db):
        from email_mcp.tools.searching import check_bulk

        _insert_message(db, "pm-body", body="Click to unsubscribe from our list.")
        # headers_indexed = 0 (default), no newsletter_id
        assert check_bulk(["pm-body"], db) == {"pm-body"}

    def test_clean_email_not_bulk(self, db):
        from email_mcp.tools.searching import check_bulk

        _insert_message(db, "pm-clean", body="Your physio appointment is Thursday.")
        assert check_bulk(["pm-clean"], db) == set()

    def test_headers_take_precedence_over_body(self, db):
        """If headers indexed and no List-Unsubscribe, not bulk even if body has 'unsubscribe'."""
        import json

        from email_mcp.tools.searching import check_bulk

        _insert_message(db, "pm-fp", body="You can unsubscribe from this thread.")
        db.execute(
            "UPDATE messages SET parsed_headers = ?, headers_indexed = 1 WHERE pm_id = ?",
            [json.dumps({"From": "alice@ex.com"}), "pm-fp"],
        )
        db.commit()

        # Headers indexed, no List-Unsubscribe → trust headers, skip body scan
        assert check_bulk(["pm-fp"], db) == set()

    def test_empty_list_returns_empty(self, db):
        from email_mcp.tools.searching import check_bulk

        assert check_bulk([], db) == set()

    def test_mixed_batch(self, db):
        from email_mcp.tools.searching import check_bulk

        _insert_message(db, "pm-a", body="Buy now! Unsubscribe here.")
        _insert_message(db, "pm-b", body="Your appointment is confirmed.")
        _insert_message(db, "pm-c", body="Normal email content.")
        db.execute("UPDATE messages SET newsletter_id = 'nl-1' WHERE pm_id = 'pm-c'")
        db.commit()

        bulk = check_bulk(["pm-a", "pm-b", "pm-c"], db)
        assert bulk == {"pm-a", "pm-c"}


class TestBulkDownranking:
    """Bulk emails should be pushed after non-bulk in scored results."""

    def test_bulk_results_sorted_after_non_bulk(self, db):
        from email_mcp.tools.searching import apply_bulk_penalty

        _insert_message(db, "pm-promo", body="Buy now! Click to unsubscribe.")
        _insert_message(db, "pm-real", body="Your physio appointment is Thursday.")

        promo_msg = db.messages.get("pm-promo")
        real_msg = db.messages.get("pm-real")

        scored = [(0.9, promo_msg), (0.5, real_msg)]
        result = apply_bulk_penalty("physio", scored, db)

        assert result[0][1].pm_id == "pm-real"
        assert result[1][1].pm_id == "pm-promo"

    def test_bulk_keeps_rank_when_query_wants_promos(self, db):
        from email_mcp.tools.searching import apply_bulk_penalty

        _insert_message(db, "pm-promo", body="Buy now! Click to unsubscribe.")
        _insert_message(db, "pm-real", body="Your physio appointment is Thursday.")

        promo_msg = db.messages.get("pm-promo")
        real_msg = db.messages.get("pm-real")

        scored = [(0.9, promo_msg), (0.5, real_msg)]
        result = apply_bulk_penalty("newsletter deals", scored, db)

        assert result[0][1].pm_id == "pm-promo"
        assert result[1][1].pm_id == "pm-real"

    def test_no_body_treated_as_non_bulk(self, db):
        from email_mcp.tools.searching import apply_bulk_penalty

        _insert_message(db, "pm-nobody")
        msg = db.messages.get("pm-nobody")

        scored = [(0.5, msg)]
        result = apply_bulk_penalty("physio", scored, db)
        assert len(result) == 1

    def test_all_bulk_still_returned(self, db):
        from email_mcp.tools.searching import apply_bulk_penalty

        _insert_message(db, "pm-1", body="Sale! Unsubscribe here.")
        _insert_message(db, "pm-2", body="Deals! Avregistrera dig.")

        msg1 = db.messages.get("pm-1")
        msg2 = db.messages.get("pm-2")

        scored = [(0.9, msg1), (0.7, msg2)]
        result = apply_bulk_penalty("physio", scored, db)
        assert len(result) == 2

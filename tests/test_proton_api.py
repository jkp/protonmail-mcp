"""Tests for the ProtonMail API client."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from email_mcp.proton_api import (
    ProtonAPIError,
    ProtonClient,
    RateLimitError,
    derive_folder,
)

# ── derive_folder ─────────────────────────────────────────────────────────────


class TestDeriveFolder:
    def test_inbox(self) -> None:
        assert derive_folder(["0"]) == "INBOX"

    def test_archive(self) -> None:
        assert derive_folder(["6"]) == "Archive"

    def test_trash(self) -> None:
        assert derive_folder(["3"]) == "Trash"

    def test_sent(self) -> None:
        assert derive_folder(["2"]) == "Sent"

    def test_spam(self) -> None:
        assert derive_folder(["4"]) == "Spam"

    def test_drafts(self) -> None:
        assert derive_folder(["1"]) == "Drafts"

    def test_custom_folder_preferred_over_all_mail(self) -> None:
        # "5" is All Mail — should be skipped in favour of real folder
        assert derive_folder(["5", "0"]) == "INBOX"

    def test_all_mail_only(self) -> None:
        # "5" is All Mail — excluded as virtual, returns None
        assert derive_folder(["5"]) is None

    def test_unknown_label_returns_none(self) -> None:
        assert derive_folder([]) is None

    def test_custom_label_alongside_folder(self) -> None:
        # custom label "abc" + Sent — folder wins
        assert derive_folder(["abc", "2"]) == "Sent"


# ── ProtonClient ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_http() -> MagicMock:
    """Return a mock httpx.AsyncClient."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.fixture
def api(tmp_path) -> ProtonClient:
    return ProtonClient(
        username="test@proton.me",
        password="secret",
        session_path=tmp_path / "session.json",
    )


class TestProtonClient:
    async def test_label_messages_calls_correct_endpoint(self, api: ProtonClient) -> None:
        api._request = AsyncMock(return_value={"Code": 1000})
        await api.label_messages(["pm-1", "pm-2"], label_id="6")
        api._request.assert_called_once_with(
            "PUT",
            "/mail/v4/messages/label",
            json={"LabelID": "6", "IDs": ["pm-1", "pm-2"]},
        )

    async def test_mark_read(self, api: ProtonClient) -> None:
        api._request = AsyncMock(return_value={"Code": 1000})
        await api.mark_read(["pm-1"])
        api._request.assert_called_once_with(
            "PUT",
            "/mail/v4/messages/read",
            json={"IDs": ["pm-1"]},
        )

    async def test_mark_unread(self, api: ProtonClient) -> None:
        api._request = AsyncMock(return_value={"Code": 1000})
        await api.mark_unread(["pm-1"])
        api._request.assert_called_once_with(
            "PUT",
            "/mail/v4/messages/unread",
            json={"IDs": ["pm-1"]},
        )

    async def test_get_labels(self, api: ProtonClient) -> None:
        api._request = AsyncMock(
            side_effect=[
                {
                    "Code": 1000,
                    "Labels": [
                        {"ID": "custom1", "Name": "MyLabel", "Type": 3},
                    ],
                },
                {
                    "Code": 1000,
                    "Labels": [
                        {"ID": "0", "Name": "Inbox", "Type": 4},
                        {"ID": "6", "Name": "Archive", "Type": 4},
                    ],
                },
            ]
        )
        labels = await api.get_labels()
        assert len(labels) == 3
        assert labels[0]["ID"] == "custom1"
        assert labels[1]["ID"] == "0"

    async def test_get_events(self, api: ProtonClient) -> None:
        payload = {
            "Code": 1000,
            "EventID": "next-event-id",
            "More": 0,
            "Refresh": 0,
            "Messages": [],
        }
        api._request = AsyncMock(return_value=payload)
        result = await api.get_events("last-event-id")
        api._request.assert_called_once_with("GET", "/core/v4/events/last-event-id")
        assert result["EventID"] == "next-event-id"

    async def test_get_latest_event_id(self, api: ProtonClient) -> None:
        api._request = AsyncMock(return_value={"Code": 1000, "EventID": "abc123"})
        eid = await api.get_latest_event_id()
        assert eid == "abc123"
        api._request.assert_called_once_with("GET", "/core/v4/events/latest")

    async def test_rate_limit_raises(self, api: ProtonClient) -> None:
        import httpx

        response = MagicMock(spec=httpx.Response)
        response.status_code = 429
        response.headers = {"Retry-After": "30"}
        api._request = AsyncMock(side_effect=RateLimitError(retry_after=30))
        with pytest.raises(RateLimitError) as exc_info:
            await api.get_latest_event_id()
        assert exc_info.value.retry_after == 30

    async def test_api_error_raises(self, api: ProtonClient) -> None:
        api._request = AsyncMock(side_effect=ProtonAPIError(422, "Invalid request"))
        with pytest.raises(ProtonAPIError) as exc_info:
            await api.get_labels()
        assert "422" in str(exc_info.value) or "Invalid" in str(exc_info.value)

    async def test_get_message_metadata_page(self, api: ProtonClient) -> None:
        api._request = AsyncMock(
            return_value={
                "Code": 1000,
                "Messages": [
                    {"ID": "pm-1", "Subject": "Hello", "LabelIDs": ["0"]},
                    {"ID": "pm-2", "Subject": "World", "LabelIDs": ["6"]},
                ],
                "Total": 2,
            }
        )
        msgs, total = await api.get_messages(page=0, page_size=150)
        assert len(msgs) == 2
        assert total == 2
        assert msgs[0]["ID"] == "pm-1"

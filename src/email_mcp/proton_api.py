"""ProtonMail HTTP API client.

Handles authenticated REST calls to the ProtonMail API. Authentication
(SRP) is deferred to a separate auth flow; this module assumes a valid
session is available and handles token refresh transparently.

System labels (folders):
    0  Inbox
    1  Drafts
    2  Sent
    3  Trash
    4  Spam
    5  All Mail   ← virtual, skip for folder derivation
    6  Archive
    7  Scheduled
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://mail.proton.me/api"

# System label IDs → folder names (in priority order for derive_folder)
_SYSTEM_LABELS: dict[str, str] = {
    "0": "INBOX",
    "1": "Drafts",
    "2": "Sent",
    "3": "Trash",
    "4": "Spam",
    "6": "Archive",
    "7": "Scheduled",
}
_ALL_MAIL_ID = "5"


# ── Exceptions ────────────────────────────────────────────────────────────────

class ProtonAPIError(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(f"ProtonMail API error {code}: {message}")


class RateLimitError(Exception):
    def __init__(self, retry_after: int = 30) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited — retry after {retry_after}s")


class AuthError(Exception):
    pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def derive_folder(label_ids: list[str]) -> str | None:
    """Derive a human-readable folder name from a message's label ID list.

    Prefers real system labels over "All Mail" (virtual). Returns None if
    label_ids is empty.
    """
    if not label_ids:
        return None

    # Check system labels in priority order, skipping All Mail
    for label_id, name in _SYSTEM_LABELS.items():
        if label_id in label_ids:
            return name

    # All Mail only
    if _ALL_MAIL_ID in label_ids:
        return "All Mail"

    return None


# ── Client ────────────────────────────────────────────────────────────────────

class ProtonClient:
    """Async ProtonMail API client.

    Requires an authenticated session. Call authenticate() once; subsequent
    instantiations load the saved session from session_path.
    """

    def __init__(
        self,
        username: str,
        password: str,
        session_path: Path,
        base_url: str = _API_BASE,
    ) -> None:
        self._username = username
        self._password = password
        self._session_path = session_path
        self._base_url = base_url
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._uid: str | None = None
        self._load_session()

    # ── Session persistence ───────────────────────────────────────────────────

    def _load_session(self) -> None:
        if self._session_path.exists():
            data = json.loads(self._session_path.read_text())
            self._access_token = data.get("access_token")
            self._refresh_token = data.get("refresh_token")
            self._uid = data.get("uid")

    def _save_session(self) -> None:
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        self._session_path.write_text(json.dumps({
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "uid": self._uid,
        }))

    # ── Low-level HTTP ────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {"x-pm-appversion": "Other"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if self._uid:
            headers["x-pm-uid"] = self._uid

        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                f"{self._base_url}{path}",
                json=json,
                params=params,
                headers=headers,
                timeout=30.0,
            )

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 30))
            raise RateLimitError(retry_after=retry_after)

        if response.status_code == 401:
            # Token expired — attempt refresh then retry once
            await self._refresh_access_token()
            return await self._request(method, path, json=json, params=params)

        data = response.json()
        code = data.get("Code", 0)
        if code != 1000 and code != 1001:
            raise ProtonAPIError(code, data.get("Error", "unknown error"))

        return data

    async def _refresh_access_token(self) -> None:
        if not self._refresh_token or not self._uid:
            raise AuthError("No refresh token available — re-authentication required")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self._base_url}/auth/refresh",
                json={"ResponseType": "token", "GrantType": "refresh_token",
                      "RefreshToken": self._refresh_token, "RedirectURI": ""},
                headers={"x-pm-uid": self._uid, "x-pm-appversion": "Other"},
                timeout=30.0,
            )

        if response.status_code != 200:
            raise AuthError(f"Token refresh failed: HTTP {response.status_code}")

        data = response.json()
        self._access_token = data["AccessToken"]
        self._refresh_token = data["RefreshToken"]
        self._save_session()

    # ── Event loop ────────────────────────────────────────────────────────────

    async def get_latest_event_id(self) -> str:
        data = await self._request("GET", "/mail/v4/events/latest")
        return data["EventID"]

    async def get_events(self, event_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/mail/v4/events/{event_id}")

    # ── Message metadata ──────────────────────────────────────────────────────

    async def get_messages(
        self, page: int = 0, page_size: int = 150
    ) -> tuple[list[dict[str, Any]], int]:
        data = await self._request(
            "GET",
            "/mail/v4/messages",
            params={"Page": page, "PageSize": page_size, "Sort": "Time", "Desc": 1},
        )
        return data["Messages"], data.get("Total", 0)

    # ── Labels ────────────────────────────────────────────────────────────────

    async def get_labels(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/mail/v4/labels")
        return data["Labels"]

    # ── Mutations ─────────────────────────────────────────────────────────────

    async def label_messages(self, pm_ids: list[str], label_id: str) -> None:
        await self._request(
            "PUT",
            "/mail/v4/messages/label",
            json={"LabelID": label_id, "IDs": pm_ids},
        )

    async def mark_read(self, pm_ids: list[str]) -> None:
        await self._request("PUT", "/mail/v4/messages/read", json={"IDs": pm_ids})

    async def mark_unread(self, pm_ids: list[str]) -> None:
        await self._request("PUT", "/mail/v4/messages/unread", json={"IDs": pm_ids})

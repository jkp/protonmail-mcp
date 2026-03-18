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

# System label IDs → IMAP folder names
# Both "real" and "aggregate" sent/drafts labels map to the same IMAP folder in Bridge
# 0=Inbox, 1=AllDrafts→Drafts, 2=AllSent→Sent, 3=Trash, 4=Spam, 6=Archive,
# 7=Sent, 8=Drafts, 9=Outbox, 16=Snoozed
# Excluded (virtual/no matching IMAP folder): 5=AllMail, 10=Starred, 12=AllScheduled, 15=AllMail(alt)
_SYSTEM_LABELS: dict[str, str] = {
    "0": "INBOX",
    "1": "Drafts",
    "2": "Sent",
    "3": "Trash",
    "4": "Spam",
    "6": "Archive",
    "7": "Sent",
    "8": "Drafts",
    "9": "Outbox",
    "16": "Snoozed",
}


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

def derive_folder(
    label_ids: list[str],
    label_map: dict[str, dict] | None = None,
) -> str | None:
    """Derive an IMAP folder name from a message's label ID list.

    Args:
        label_ids: ProtonMail label IDs for the message.
        label_map: Optional {label_id: {name, type}} from the labels API.
                   Type=3 labels → Bridge IMAP "Labels/{name}"
                   Type=4 labels are system labels already in _SYSTEM_LABELS.

    Prefers real system folders. Falls back to user labels (Type=3).
    Returns None if unresolvable (e.g., only virtual aggregate labels).
    """
    if not label_ids:
        return None

    # Check real system folders first (Type=4, non-virtual)
    for label_id, name in _SYSTEM_LABELS.items():
        if label_id in label_ids:
            return name

    # User-created labels (Type=3) → Bridge exposes as "Labels/{name}"
    if label_map:
        for label_id in label_ids:
            info = label_map.get(label_id)
            if info and info.get("type") == 3:
                return f"Labels/{info['name']}"

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
        data = await self._request("GET", "/core/v4/events/latest")
        return data["EventID"]

    async def get_events(self, event_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/core/v4/events/{event_id}")

    # ── User / keys ──────────────────────────────────────────────────────────

    async def get_user(self) -> dict[str, Any]:
        """GET /core/v4/users → User dict with Keys[] and other account info."""
        data = await self._request("GET", "/core/v4/users")
        return data["User"]

    async def get_addresses(self) -> list[dict[str, Any]]:
        """GET /core/v4/addresses → list of address dicts with Keys[]."""
        data = await self._request("GET", "/core/v4/addresses")
        return data["Addresses"]

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

    async def get_message(self, pm_id: str) -> dict[str, Any]:
        """GET /mail/v4/messages/{pm_id} → full message with encrypted Body."""
        data = await self._request("GET", f"/mail/v4/messages/{pm_id}")
        return data["Message"]

    # ── Labels ────────────────────────────────────────────────────────────────

    async def get_labels(self) -> list[dict[str, Any]]:
        # Type=3 = user-created labels (Bridge: "Labels/{name}")
        # Type=4 = system folders (INBOX, Sent, Trash, etc.)
        user_labels = await self._request("GET", "/core/v4/labels", params={"Type": 3})
        system = await self._request("GET", "/core/v4/labels", params={"Type": 4})
        return user_labels["Labels"] + system["Labels"]

    # ── Attachments ─────────────────────────────────────────────────────────

    async def get_attachment(self, att_id: str) -> bytes:
        """GET /mail/v4/attachments/{att_id} → raw encrypted attachment bytes."""
        response = await self._http.get(
            f"{self._base_url}/mail/v4/attachments/{att_id}",
            headers=self._auth_headers(),
        )
        if response.status_code == 401:
            await self._refresh_access_token()
            response = await self._http.get(
                f"{self._base_url}/mail/v4/attachments/{att_id}",
                headers=self._auth_headers(),
            )
        response.raise_for_status()
        return response.content

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

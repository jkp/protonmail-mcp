"""Notmuch search with Maildir UID extraction for bridging to himalaya IMAP."""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import structlog

from protonmail_mcp.models import SearchResult

logger = structlog.get_logger()

_UID_PATTERN = re.compile(r",U=(\d+)")


class NotmuchError(Exception):
    """Error from notmuch CLI execution."""


def extract_uid(filepath: str) -> str | None:
    """Extract IMAP UID from mbsync Maildir filename (,U=<uid> scheme)."""
    match = _UID_PATTERN.search(filepath)
    return match.group(1) if match else None


def extract_folder(filepath: str, maildir_root: str) -> str:
    """Extract folder name from a Maildir file path.

    Given /home/user/mail/INBOX/cur/filename and root /home/user/mail,
    returns 'INBOX'. For nested folders like Work/Projects/cur/filename,
    returns 'Work/Projects'.
    """
    root = Path(maildir_root)
    full = Path(filepath)
    try:
        relative = full.relative_to(root)
    except ValueError:
        return ""
    parts = relative.parts
    # Folder is everything except the last two components (cur|new|tmp + filename)
    if len(parts) >= 3:
        return "/".join(parts[:-2])
    return parts[0] if parts else ""


class NotmuchSearcher:
    """Wraps notmuch CLI for search with UID extraction from Maildir paths."""

    def __init__(
        self,
        bin_path: str = "notmuch",
        config_path: str | None = None,
        maildir_root: str = "",
        timeout: int = 30,
    ) -> None:
        self.bin_path = bin_path
        self.config_path = config_path
        self.maildir_root = maildir_root
        self.timeout = timeout

    async def _run(self, *args: str) -> str:
        """Run a notmuch command and return stdout."""
        cmd = [self.bin_path, *args]
        logger.debug("notmuch.run", cmd=cmd)

        env = None
        if self.config_path:
            import os

            env = {**os.environ, "NOTMUCH_CONFIG": self.config_path}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self.timeout,
            )
        except TimeoutError:
            proc.kill()
            raise NotmuchError(f"notmuch command timed out after {self.timeout}s")

        if proc.returncode != 0:
            raise NotmuchError(stderr_bytes.decode().strip())

        return stdout_bytes.decode()

    async def search(
        self,
        query: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SearchResult]:
        """Search notmuch and return results with IMAP UIDs and folders.

        1. notmuch search --output=files <query> → Maildir file paths
        2. Extract UID from filename (,U=<uid>)
        3. Extract folder from path
        4. Return structured results
        """
        raw = await self._run("search", "--output=files", query)
        if not raw.strip():
            return []

        filepaths = raw.strip().split("\n")
        results: list[SearchResult] = []

        for filepath in filepaths:
            uid = extract_uid(filepath)
            if uid is None:
                continue
            folder = extract_folder(filepath, self.maildir_root)
            results.append(SearchResult(uid=uid, folder=folder))

        # Apply offset and limit
        results = results[offset:]
        if limit is not None:
            results = results[:limit]

        return results

    async def search_threads(self, query: str) -> list[dict[str, Any]]:
        """Search notmuch for thread-level summaries (JSON format)."""
        raw = await self._run("search", "--format=json", query)
        try:
            return json.loads(raw)  # type: ignore[no-any-return]
        except json.JSONDecodeError as e:
            raise NotmuchError(f"Failed to parse notmuch JSON output: {e}") from e

"""Notmuch search with Maildir UID extraction for bridging to himalaya IMAP."""

import asyncio
import json
import re
import time
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


def _first_matching_message(thread: list) -> dict | None:
    """Extract the first matching message from a notmuch show thread tree.

    The notmuch show JSON structure is: [[[message, [replies]], ...], ...]
    where each message is a dict with 'match', 'headers', 'filename', etc.
    """
    if not thread:
        return None
    for item in thread:
        if isinstance(item, dict) and item.get("match"):
            return item
        if isinstance(item, list):
            result = _first_matching_message(item)
            if result is not None:
                return result
    return None


class NotmuchSearcher:
    """Wraps notmuch CLI for search with UID extraction from Maildir paths."""

    def __init__(
        self,
        bin_path: str = "notmuch",
        config_path: str | None = None,
        maildir_root: str = "",
        timeout: int = 30,
    ) -> None:
        import os

        self.bin_path = bin_path
        self.config_path = config_path
        self.maildir_root = os.path.expanduser(maildir_root) if maildir_root else ""
        self.timeout = timeout

    async def _run(self, *args: str) -> str:
        """Run a notmuch command and return stdout."""
        cmd = [self.bin_path, *args]
        log = logger.bind(subcommand=args[0] if args else "")
        log.debug("notmuch.exec", cmd=cmd)

        env = None
        if self.config_path:
            import os

            env = {**os.environ, "NOTMUCH_CONFIG": os.path.expanduser(self.config_path)}

        t0 = time.monotonic()
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
            elapsed = time.monotonic() - t0
            log.error("notmuch.timeout", elapsed_s=round(elapsed, 2), timeout=self.timeout)
            raise NotmuchError(f"notmuch command timed out after {self.timeout}s")

        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            stderr_str = stderr_bytes.decode().strip()
            log.error(
                "notmuch.error",
                returncode=proc.returncode,
                stderr=stderr_str,
                elapsed_s=round(elapsed, 2),
            )
            raise NotmuchError(stderr_str)

        log.info("notmuch.ok", elapsed_s=round(elapsed, 2), bytes=len(stdout_bytes))
        return stdout_bytes.decode()

    async def search(
        self,
        query: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SearchResult]:
        """Search notmuch and return results with IMAP UIDs, folders, and metadata.

        Uses `notmuch show --body=false` to get per-message metadata (subject,
        from, date) alongside filenames for UID/folder extraction in one call.
        """
        args = ["show", "--format=json", "--body=false"]
        if limit is not None:
            args.extend(["--limit", str(offset + limit)])
        if offset and limit is None:
            args.extend(["--limit", str(offset + 100)])
        args.append(query)

        raw = await self._run(*args)
        if not raw.strip():
            return []

        try:
            threads = json.loads(raw)
        except json.JSONDecodeError as e:
            raise NotmuchError(f"Failed to parse notmuch JSON output: {e}") from e

        results: list[SearchResult] = []
        for thread in threads:
            msg = _first_matching_message(thread)
            if msg is None:
                continue
            filenames = msg.get("filename", [])
            if not filenames:
                continue
            filepath = filenames[0]
            uid = extract_uid(filepath)
            if uid is None:
                continue
            folder = extract_folder(filepath, self.maildir_root)
            headers = msg.get("headers", {})
            results.append(SearchResult(
                uid=uid,
                folder=folder,
                subject=headers.get("Subject", ""),
                date=headers.get("Date", ""),
                authors=headers.get("From", ""),
            ))

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

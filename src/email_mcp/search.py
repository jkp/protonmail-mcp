"""Notmuch search with luqum Gmail-style query translation."""

import asyncio
import json
import re
import time

import structlog
from luqum.parser import parser as luqum_parser
from luqum.tree import Not, SearchField, Word
from luqum.visitor import TreeTransformer

from email_mcp.models import SearchResult

logger = structlog.get_logger()

# Gmail-style folder name → IMAP folder name
_FOLDER_CASE_MAP = {
    "inbox": "INBOX",
    "sent": "Sent",
    "drafts": "Drafts",
    "trash": "Trash",
    "archive": "Archive",
    "spam": "Spam",
    "starred": "Starred",
}

# Gmail is: values → notmuch tag names
_IS_TAG_MAP = {
    "unread": "unread",
    "starred": "flagged",
    "flagged": "flagged",
}


class _GmailToNotmuch(TreeTransformer):
    """Rewrite Gmail-style search fields to notmuch equivalents."""

    def visit_search_field(self, node, context):  # type: ignore[no-untyped-def]
        new_node = node.clone_item()
        new_node.children = list(self.clone_children(node, new_node, context))
        expr = new_node.expr

        if new_node.name == "has" and isinstance(expr, Word) and expr.value == "attachment":
            new_node.name = "tag"
        elif new_node.name == "label":
            new_node.name = "tag"
        elif new_node.name == "filename":
            new_node.name = "attachment"
        elif new_node.name == "in" and isinstance(expr, Word):
            new_node.name = "folder"
            corrected = Word(_FOLDER_CASE_MAP.get(expr.value.lower(), expr.value))
            corrected.head = expr.head
            corrected.tail = expr.tail
            new_node.expr = corrected
        elif new_node.name == "is" and isinstance(expr, Word):
            if expr.value == "read":
                child = SearchField("tag", Word("unread"))
                child.head = " "
                not_node = Not(child)
                not_node.head = new_node.head
                yield not_node
                return
            tag = _IS_TAG_MAP.get(expr.value, expr.value)
            new_node.name = "tag"
            new_node.expr = Word(tag)
        elif new_node.name == "newer_than" and isinstance(expr, Word):
            m = re.match(r"(\d+)d", expr.value)
            if m:
                new_node.name = "date"
                new_node.expr = Word(f"{m.group(1)}days..")
        elif new_node.name == "older_than" and isinstance(expr, Word):
            m = re.match(r"(\d+)d", expr.value)
            if m:
                new_node.name = "date"
                new_node.expr = Word(f"..{m.group(1)}days")

        yield new_node


_transformer = _GmailToNotmuch()


def translate_query(query: str) -> str:
    """Translate Gmail-style search operators to notmuch syntax via AST rewriting."""
    tree = luqum_parser.parse(query)
    transformed = _transformer.visit(tree)
    return str(transformed)


class NotmuchError(Exception):
    """Error from notmuch execution."""


def _first_matching_message(thread: list) -> dict | None:  # type: ignore[type-arg]
    """Extract the first matching message from a notmuch show thread tree."""
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
    """Search via notmuch CLI subprocess."""

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
        """Search notmuch and return results with Message-IDs.

        Uses `notmuch show --body=false` to get per-message metadata.
        Returns Message-IDs instead of IMAP UIDs.
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
            message_id = msg.get("id", "")
            if not message_id:
                continue
            headers = msg.get("headers", {})
            tags = set(msg.get("tags", []))

            # Extract folder from filename
            filenames = msg.get("filename", [])
            folder = ""
            if filenames and self.maildir_root:
                from pathlib import Path

                filepath = filenames[0]
                try:
                    relative = Path(filepath).relative_to(self.maildir_root)
                    parts = relative.parts
                    if len(parts) >= 3:
                        folder = "/".join(parts[:-2])
                    elif parts:
                        folder = parts[0]
                except ValueError:
                    pass

            results.append(SearchResult(
                message_id=message_id,
                folder=folder,
                subject=headers.get("Subject", ""),
                date=headers.get("Date", ""),
                authors=headers.get("From", ""),
                tags=tags,
            ))

        results = results[offset:]
        if limit is not None:
            results = results[:limit]

        return results

    async def count(self, query: str) -> int:
        """Count messages matching a query."""
        raw = await self._run("count", query)
        return int(raw.strip())

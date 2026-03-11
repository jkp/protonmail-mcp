"""Maildir operations: read, list, move, delete, flag management."""

import email
import email.policy
import email.utils
import re
from datetime import UTC, datetime
from pathlib import Path

import structlog

from email_mcp.convert import html_to_markdown
from email_mcp.models import Address, Attachment, Email, Folder

logger = structlog.get_logger()

_FLAGS_PATTERN = re.compile(r":2,([A-Z]*)$")

# Maildir flag characters
FLAG_SEEN = "S"
FLAG_REPLIED = "R"
FLAG_FLAGGED = "F"
FLAG_DRAFT = "D"
FLAG_TRASHED = "T"


def _parse_address(addr_str: str) -> Address:
    """Parse an email address string into an Address model."""
    if not addr_str:
        return Address()
    name, addr = email.utils.parseaddr(addr_str)
    return Address(name=name, addr=addr)


def _parse_address_list(header: str | None) -> list[Address]:
    """Parse a comma-separated address header into a list of Address models."""
    if not header:
        return []
    pairs = email.utils.getaddresses([header])
    return [Address(name=name, addr=addr) for name, addr in pairs if addr]


def _get_flags(path: Path) -> str:
    """Extract Maildir flags from a filename."""
    match = _FLAGS_PATTERN.search(path.name)
    return match.group(1) if match else ""


def _set_flags(path: Path, flags: str) -> Path:
    """Rename a Maildir file to change its flags. Returns the new path."""
    name = path.name
    sorted_flags = "".join(sorted(set(flags)))
    match = _FLAGS_PATTERN.search(name)
    if match:
        new_name = name[: match.start()] + f":2,{sorted_flags}"
    else:
        new_name = f"{name}:2,{sorted_flags}"
    new_path = path.parent / new_name
    if new_path != path:
        path.rename(new_path)
    return new_path


def _extract_body(msg: email.message.EmailMessage) -> tuple[str, str]:
    """Extract plain text and HTML body from an email message."""
    plain = ""
    html = ""

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            if ct == "text/plain" and not plain:
                payload = part.get_content()
                plain = payload if isinstance(payload, str) else ""
            elif ct == "text/html" and not html:
                payload = part.get_content()
                html = payload if isinstance(payload, str) else ""
    else:
        ct = msg.get_content_type()
        payload = msg.get_content()
        text = payload if isinstance(payload, str) else ""
        if ct == "text/html":
            html = text
        else:
            plain = text

    return plain, html


def _extract_attachments(msg: email.message.EmailMessage) -> list[Attachment]:
    """Extract attachment metadata from an email message."""
    attachments = []
    for part in msg.walk():
        disp = str(part.get("Content-Disposition", ""))
        if "attachment" not in disp and "inline" not in disp:
            continue
        filename = part.get_filename()
        if not filename:
            continue
        ct = part.get_content_type()
        payload = part.get_payload(decode=True)
        size = len(payload) if payload else 0
        attachments.append(Attachment(filename=filename, content_type=ct, size=size))
    return attachments


def _parse_date(msg: email.message.EmailMessage) -> tuple[datetime | None, str]:
    """Parse the Date header into a datetime and original string."""
    date_str = msg.get("Date", "")
    if not date_str:
        return None, ""
    parsed = email.utils.parsedate_to_datetime(date_str)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed, date_str


class MaildirStore:
    """Read, list, move, delete, and flag operations on a Maildir."""

    def __init__(self, maildir_root: Path) -> None:
        self.root = maildir_root

    def list_folders(self) -> list[Folder]:
        """List all folders in the Maildir."""
        if not self.root.exists():
            return []

        folders = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith("."):
                continue
            # Check if it's a valid Maildir folder (has cur/ subdirectory)
            if not (entry / "cur").is_dir():
                continue
            cur_count = sum(1 for _ in (entry / "cur").iterdir())
            new_count = sum(1 for _ in (entry / "new").iterdir()) if (entry / "new").is_dir() else 0
            folders.append(Folder(
                name=entry.name,
                path=str(entry),
                count=cur_count + new_count,
                unread=new_count,
            ))
        return folders

    def _find_message_files(self, folder: str) -> list[Path]:
        """List all message files in a folder (cur + new)."""
        folder_path = self.root / folder
        files = []
        for subdir in ("cur", "new"):
            sub = folder_path / subdir
            if sub.is_dir():
                files.extend(f for f in sub.iterdir() if f.is_file())
        return files

    def _find_file_by_message_id(self, message_id: str, folder: str | None = None) -> Path | None:
        """Find the Maildir file for a given Message-ID.

        Searches the specified folder, or all folders if not specified.
        """
        folders = [folder] if folder else [f.name for f in self.list_folders()]
        for f in folders:
            for path in self._find_message_files(f):
                msg = self._quick_parse_headers(path)
                if msg and msg.get("Message-ID", "").strip() == message_id:
                    return path
        return None

    def _quick_parse_headers(self, path: Path) -> email.message.EmailMessage | None:
        """Parse only the headers of an email file."""
        try:
            with path.open("rb") as f:
                return email.parser.BytesParser(
                    policy=email.policy.default
                ).parse(f, headersonly=True)
        except Exception:
            logger.debug("store.parse_header_failed", path=str(path))
            return None

    def _parse_file(self, path: Path) -> email.message.EmailMessage | None:
        """Parse a full email file."""
        try:
            with path.open("rb") as f:
                return email.parser.BytesParser(
                    policy=email.policy.default
                ).parse(f)
        except Exception:
            logger.debug("store.parse_failed", path=str(path))
            return None

    def _folder_from_path(self, path: Path) -> str:
        """Extract folder name from a file path relative to maildir root."""
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return ""
        parts = relative.parts
        if len(parts) >= 3:
            return "/".join(parts[:-2])
        return parts[0] if parts else ""

    def read_email(self, message_id: str, folder: str | None = None) -> Email | None:
        """Read a full email by Message-ID."""
        path = self._find_file_by_message_id(message_id, folder)
        if path is None:
            return None

        msg = self._parse_file(path)
        if msg is None:
            return None

        plain, html_body = _extract_body(msg)
        body_markdown = html_to_markdown(html_body) if html_body else ""
        date, date_str = _parse_date(msg)
        attachments = _extract_attachments(msg)
        flags = _get_flags(path)
        detected_folder = self._folder_from_path(path)

        return Email(
            message_id=message_id,
            folder=detected_folder,
            path=str(path),
            from_=_parse_address(msg.get("From", "")),
            to=_parse_address_list(msg.get("To")),
            cc=_parse_address_list(msg.get("Cc")),
            bcc=_parse_address_list(msg.get("Bcc")),
            subject=msg.get("Subject", ""),
            date=date,
            date_str=date_str,
            body_plain=plain,
            body_html=body_markdown if body_markdown else plain,
            attachments=attachments,
            tags=set(),
            flags=flags,
        )

    def list_emails(self, folder: str = "INBOX", limit: int = 20, offset: int = 0) -> list[Email]:
        """List email summaries in a folder (headers only, no body parsing)."""
        files = self._find_message_files(folder)
        # Sort by modification time, newest first
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        files = files[offset: offset + limit]

        emails = []
        for path in files:
            msg = self._quick_parse_headers(path)
            if msg is None:
                continue
            mid = msg.get("Message-ID", "").strip()
            date, date_str = _parse_date(msg)
            flags = _get_flags(path)
            detected_folder = self._folder_from_path(path)
            emails.append(Email(
                message_id=mid,
                folder=detected_folder,
                path=str(path),
                from_=_parse_address(msg.get("From", "")),
                to=_parse_address_list(msg.get("To")),
                subject=msg.get("Subject", ""),
                date=date,
                date_str=date_str,
                flags=flags,
            ))
        return emails

    def move_email(self, message_id: str, to_folder: str, from_folder: str | None = None) -> bool:
        """Move an email to a different folder."""
        path = self._find_file_by_message_id(message_id, from_folder)
        if path is None:
            return False

        dest_dir = self.root / to_folder / "cur"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / path.name
        path.rename(dest_path)
        logger.info("store.moved", message_id=message_id, to_folder=to_folder)
        return True

    def delete_email(self, message_id: str, folder: str | None = None) -> bool:
        """Move an email to Trash."""
        return self.move_email(message_id, "Trash", folder)

    def archive_email(self, message_id: str, folder: str | None = None) -> bool:
        """Move an email to Archive."""
        return self.move_email(message_id, "Archive", folder)

    def set_flags(self, message_id: str, flags: str, folder: str | None = None) -> bool:
        """Set flags on an email (overwrites existing flags)."""
        path = self._find_file_by_message_id(message_id, folder)
        if path is None:
            return False
        _set_flags(path, flags)
        return True

    def add_flag(self, message_id: str, flag: str, folder: str | None = None) -> bool:
        """Add a flag to an email."""
        path = self._find_file_by_message_id(message_id, folder)
        if path is None:
            return False
        existing = _get_flags(path)
        _set_flags(path, existing + flag)
        return True

    def remove_flag(self, message_id: str, flag: str, folder: str | None = None) -> bool:
        """Remove a flag from an email."""
        path = self._find_file_by_message_id(message_id, folder)
        if path is None:
            return False
        existing = _get_flags(path)
        _set_flags(path, existing.replace(flag, ""))
        return True

    def get_attachment_content(
        self, message_id: str, filename: str, folder: str | None = None
    ) -> tuple[bytes, str] | None:
        """Get attachment content by filename. Returns (content, content_type) or None."""
        path = self._find_file_by_message_id(message_id, folder)
        if path is None:
            return None

        msg = self._parse_file(path)
        if msg is None:
            return None

        for part in msg.walk():
            if part.get_filename() == filename:
                payload = part.get_payload(decode=True)
                if payload is not None:
                    return payload, part.get_content_type()
        return None

    def save_message(self, folder: str, raw_bytes: bytes) -> Path:
        """Save a raw email message to a Maildir folder."""
        import time

        cur_dir = self.root / folder / "cur"
        cur_dir.mkdir(parents=True, exist_ok=True)

        # Generate a unique Maildir filename
        timestamp = time.time()
        hostname = "localhost"
        unique = f"{timestamp:.6f}.{id(raw_bytes)}.{hostname}:2,S"
        dest = cur_dir / unique
        dest.write_bytes(raw_bytes)
        logger.info("store.saved", folder=folder, path=str(dest))
        return dest

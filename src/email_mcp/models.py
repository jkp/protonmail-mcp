"""Pydantic models for email data structures."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class Address(BaseModel):
    name: str = ""
    addr: str = ""

    def __str__(self) -> str:
        if self.name:
            return f"{self.name} <{self.addr}>"
        return self.addr


class Attachment(BaseModel):
    filename: str
    content_type: str
    size: int = 0


class Email(BaseModel):
    message_id: str
    folder: str = ""
    path: str = ""
    from_: Address = Address()
    to: list[Address] = []
    cc: list[Address] = []
    bcc: list[Address] = []
    subject: str = ""
    date: datetime | None = None
    date_str: str = ""
    body_plain: str = ""
    body_html: str = ""
    attachments: list[Attachment] = []
    tags: set[str] = set()
    flags: str = ""


class Folder(BaseModel):
    name: str
    path: str = ""
    count: int = 0
    unread: int = 0


class SearchResult(BaseModel):
    message_id: str
    folder: str = ""
    subject: str = ""
    date: str = ""
    authors: str = ""
    tags: set[str] = set()


class SyncStatus(BaseModel):
    state: Literal["initializing", "syncing", "ready", "error"] = "initializing"
    last_sync: datetime | None = None
    last_index: datetime | None = None
    message_count: int = 0
    error: str | None = None

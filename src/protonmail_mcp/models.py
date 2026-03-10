"""Pydantic models for email data structures."""

from pydantic import BaseModel, Field, field_validator


class Address(BaseModel):
    name: str | None = ""
    addr: str

    def __str__(self) -> str:
        if self.name:
            return f"{self.name} <{self.addr}>"
        return self.addr


def _coerce_address_list(v: object) -> list[Address]:
    """Accept a single Address dict or a list of them."""
    if isinstance(v, dict):
        return [Address.model_validate(v)]
    if isinstance(v, Address):
        return [v]
    if isinstance(v, list):
        return [Address.model_validate(item) if isinstance(item, dict) else item for item in v]
    return []


class Attachment(BaseModel):
    filename: str
    content_type: str = Field(alias="content-type")
    size: int = 0

    model_config = {"populate_by_name": True}


class Envelope(BaseModel):
    id: str
    from_: Address = Field(alias="from")
    to: list[Address] = []
    subject: str = ""
    date: str = ""
    has_attachment: bool = False

    model_config = {"populate_by_name": True}

    @field_validator("to", mode="before")
    @classmethod
    def coerce_to(cls, v: object) -> list[Address]:
        return _coerce_address_list(v)


class Message(BaseModel):
    id: str
    from_: Address = Field(alias="from")
    to: list[Address] = []
    cc: list[Address] = []
    bcc: list[Address] = []
    subject: str = ""
    date: str = ""
    text_plain: str | None = Field(default=None, alias="text/plain")
    text_html: str | None = Field(default=None, alias="text/html")
    attachments: list[Attachment] = []

    model_config = {"populate_by_name": True}

    @field_validator("to", "cc", "bcc", mode="before")
    @classmethod
    def coerce_address_fields(cls, v: object) -> list[Address]:
        return _coerce_address_list(v)


class Folder(BaseModel):
    name: str
    desc: str = ""


class SearchResult(BaseModel):
    uid: str
    folder: str
    subject: str = ""
    date: str = ""
    authors: str = ""

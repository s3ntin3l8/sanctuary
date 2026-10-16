"""Persisted transport evidence, distinct from a document's legal family tree."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ReceiptSource = Literal["gmail", "email_date", "ingest_time", "legacy_unknown"]
PartOutcome = Literal[
    "pending",
    "ingested",
    "duplicate",
    "ignored_inline",
    "empty",
    "unsupported",
    "failed",
    "nested_source",
    "body_context",
    "removed",
]


class EmailPart(BaseModel):
    id: str
    filename: str | None = None
    original_filename: str | None = None
    mime_type: str
    disposition: str | None = None
    size: int = 0
    outcome: PartOutcome = "pending"
    reason: str | None = None
    document_ids: list[int] = Field(default_factory=list)


class EmailManifestEntry(BaseModel):
    filename: str
    timestamp: str | None = None
    source_label: str
    raw_timestamp: str | None = None
    historical: bool = False
    part_id: str | None = None
    doc_id: int | None = None
    group_id: str | None = None
    issue: str | None = None


class EmailSource(BaseModel):
    id: str
    parent_id: str | None = None
    sender: str = ""
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    reply_to: list[str] = Field(default_factory=list)
    subject: str = ""
    message_id: str = ""
    in_reply_to: str | None = None
    thread_refs: list[str] = Field(default_factory=list)
    date: str = ""
    sent_at: datetime | None = None
    email_note: str = ""
    routing_hints: list[str] | None = None
    attachment_manifest: list[EmailManifestEntry] = Field(default_factory=list)
    parts: list[EmailPart] = Field(default_factory=list)


class EmailProvenance(BaseModel):
    batch_id: int
    received_at: datetime
    received_source: ReceiptSource
    gmail_id: str | None = None
    sources: list[EmailSource] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    acknowledged: bool = False

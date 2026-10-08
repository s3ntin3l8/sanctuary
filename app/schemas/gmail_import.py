"""Gmail history import: mailbox index, case-reference groups, import runs."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class GmailIndexStatus(BaseModel):
    running: bool
    indexed_count: int
    last_indexed_at: datetime | None
    # Progress of the running refresh (new messages being fetched).
    done: int
    total: int
    # Messages Gmail didn't return (quota/5xx) or that couldn't be parsed this
    # run; a further refresh retries them.
    skipped: int
    error: str | None
    # Raw messages kept locally (data/gmail_raw): re-importing them needs no Gmail.
    cached_count: int
    cached_bytes: int


class GmailGroup(BaseModel):
    # The case reference found in subjects ("8372-25" file number / "3 F 426/25" court Az.), or
    # "unreferenced" for mail carrying none.
    key: str
    kind: Literal["internal_id", "az_court"] | None
    # Existing case this group files into on import (None = no case yet).
    matched_case_id: str | None
    count: int
    ingested_count: int
    first_at: datetime
    last_at: datetime


class GmailGroupList(BaseModel):
    groups: list[GmailGroup]


class GmailIndexedMessage(BaseModel):
    gmail_id: str
    thread_id: str
    sender: str | None
    subject: str | None
    sent_at: datetime
    has_attachments: bool
    ingested: bool
    cached: bool


class GmailMessagePage(BaseModel):
    items: list[GmailIndexedMessage]
    next_cursor: str | None


class GmailNewMessages(BaseModel):
    """Mail that arrived after the sync point and isn't imported yet (the banner)."""

    count: int
    # "auto" imports new mail itself, so the UI doesn't offer it for review.
    sync_mode: Literal["off", "notify", "auto"]
    # The sync point: only mail received after it counts as new.
    since: datetime | None
    # When the last background/manual check reached Gmail.
    checked_at: datetime | None
    # The oldest 100, oldest first.
    items: list[GmailIndexedMessage]


class GmailImportRequest(BaseModel):
    # An explicit pick wins over `group`; oldest_n/before narrow either.
    gmail_ids: list[str] | None = Field(default=None, max_length=500)
    group: str | None = None
    oldest_n: int | None = Field(default=None, ge=1, le=100)
    before: date | None = None
    # Narrow to mail that arrived after the sync point (the "new mail" banner).
    new: bool = False
    # Wait for each email's documents to finish processing before ingesting the
    # next, so earlier letters are enriched before their replies arrive.
    sequential: bool = True


class GmailImportQueued(BaseModel):
    queued: int


class GmailImportStatus(BaseModel):
    active: bool
    total: int
    done: int
    failed_count: int
    sequential: bool
    cancelled: bool
    error: str | None
    current_subject: str | None
    # Sequential mode: waiting for the last email's documents to finish.
    waiting: bool
    started_at: datetime | None
    finished_at: datetime | None

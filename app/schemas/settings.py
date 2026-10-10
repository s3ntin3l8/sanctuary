"""Settings tabs: account, Gmail, identity, AI & models, appearance, data."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import UserRole
from app.services.ingestion.gmail import LABEL_FORBIDDEN

# --- Account -----------------------------------------------------------------


class AccountView(BaseModel):
    email: str
    display_name: str | None
    role: UserRole
    has_password: bool


class ProfileUpdate(BaseModel):
    display_name: str = ""


class EmailChange(BaseModel):
    new_email: str
    current_password: str = ""


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


# --- Gmail -------------------------------------------------------------------

# off: nothing in the background · notify: index new mail and offer it for import ·
# auto: import new mail as it arrives.
SyncMode = Literal["off", "notify", "auto"]


class GmailView(BaseModel):
    connected: bool
    connected_at: str | None
    last_sync_at: str | None
    allowlist: list[str]
    label_filter: str
    oauth_start_url: str
    sync_mode: SyncMode
    # When the last new-mail check (notify mode) reached Gmail.
    last_check_at: str | None
    last_sync_result: str | None
    last_sync_error: str | None
    # The stored grant is unusable; only reconnecting fixes it.
    reconnect_required: bool
    failed_count: int
    # An active AI endpoint (chat/embed/OCR) is on the public internet, so
    # imported mail would leave this machine.
    ai_external: bool


class GmailSyncMode(BaseModel):
    mode: SyncMode


class GmailResetSync(BaseModel):
    # Move the sync watermark here; omitted = now. Never cleared.
    since: date | None = None

    @field_validator("since")
    @classmethod
    def _not_in_the_future(cls, value: date | None) -> date | None:
        # A future watermark would silently skip every message until then.
        if value is not None and value > datetime.now(UTC).date():
            raise ValueError("since can't be in the future")
        return value


# A domain ("firm.de", "@firm.de") or an address ("a@firm.de"). Letters, digits and
# a few separators only — no ":" or spaces, so "from:x@y.de", "in:anywhere" or
# "a OR b" can never be smuggled in as an operator.
_DOMAIN = r"([A-Za-z0-9-]+\.)+[A-Za-z]{2,}"
_SENDER_RE = re.compile(rf"^(@?{_DOMAIN}|[A-Za-z0-9._%+-]+@{_DOMAIN})$")


class GmailFilters(BaseModel):
    allowlist: list[str]
    label_filter: str = ""

    @model_validator(mode="after")
    def _clean_and_require_a_filter(self) -> GmailFilters:
        # Mail is only ever pulled for a bounded slice of the mailbox, so a
        # filter is mandatory: senders, a label, or both.
        self.allowlist = [e.strip() for e in self.allowlist if e.strip()]
        self.label_filter = self.label_filter.strip()
        for entry in self.allowlist:
            if not _SENDER_RE.match(entry):
                raise ValueError(
                    f"'{entry}' isn't an email address or domain "
                    "(e.g. lawyer@firm.de or firm.de)"
                )
        if any(c in self.label_filter for c in LABEL_FORBIDDEN):
            raise ValueError("The label can't contain quotes or line breaks")
        if not self.allowlist and not self.label_filter:
            raise ValueError("Set a sender allowlist or a label — or both")
        return self


class GmailFilterPreview(BaseModel):
    estimate: int


# --- Identity & context (global) ---------------------------------------------


class IdentityView(BaseModel):
    own_self: str
    own_parties: list[str]
    user_context: str


# --- AI & models (global) -----------------------------------------------------

Role = Literal["chat", "embed", "ocr"]


class AiHealth(BaseModel):
    ok: bool
    provider: str | None = None
    detail: str


class AiInstance(BaseModel):
    id: str
    label: str
    base_url: str
    has_api_key: bool
    is_external: bool
    summary_model: str
    embed_model: str
    embed_dim: int | None
    ocr_model: str


class AiInstanceInput(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=500)
    # None keeps the stored key; "" clears it.
    api_key: str | None = None


class AiRole(BaseModel):
    role: Role
    label: str
    hint: str
    active_id: str | None
    model: str
    embed_dim: int | None


class EmbedIndex(BaseModel):
    dim: int
    model: str
    index_dim: int | None
    mismatch: bool


class ReindexJob(BaseModel):
    status: Literal["running", "done", "failed"]
    total: int = 0
    reindexed: int = 0
    failed: int = 0
    started_at: str | None = None
    ended_at: str | None = None
    embed_dim: int | None = None
    error: str | None = None


class AiSettingsView(BaseModel):
    instances: list[AiInstance]
    roles: list[AiRole]
    extraction_engine: Literal["chandra", "docling"]
    tesseract_available: bool
    worker_concurrency: int
    ocr_concurrency: int
    embed_index: EmbedIndex
    reindex_job: ReindexJob | None


class RoleHealthView(BaseModel):
    """Live health per role; a separate call because it talks to the endpoints."""

    chat: AiHealth
    embed: AiHealth
    ocr: AiHealth


class ModelsView(BaseModel):
    chat: list[str]
    embed: list[str]
    ocr: list[str]


class RoleUpdate(BaseModel):
    instance_id: str
    # Empty = switch the endpoint only; set = pick this model on it.
    model: str = ""


class RoleUpdateResult(BaseModel):
    role: AiRole
    health: AiHealth
    warning: str | None
    embed_index: EmbedIndex


class ConcurrencyUpdate(BaseModel):
    concurrency: int = Field(ge=1, le=16)


class ConcurrencyResult(BaseModel):
    concurrency: int
    applied_live: bool


class ExtractionEngineUpdate(BaseModel):
    engine: Literal["chandra", "docling"]


class DebugRedactUpdate(BaseModel):
    enabled: bool


# --- Appearance --------------------------------------------------------------


class DashboardCards(BaseModel):
    action_items: bool = True
    costs: bool = True
    documents: bool = True


class AppearanceView(BaseModel):
    theme: Literal["dark", "light"]
    dashboard_cards: DashboardCards
    timezone: str
    timezone_choices: list[str]


class ThemeUpdate(BaseModel):
    theme: Literal["dark", "light"]


class TimezoneUpdate(BaseModel):
    tz: str


# --- Data --------------------------------------------------------------------


class DataView(BaseModel):
    doc_count: int
    case_count: int
    claim_count: int
    cost_count: int
    db_size_mb: float
    ai_debug_redact: bool


class MaintenanceResult(BaseModel):
    message: str


class DebugLogRow(BaseModel):
    ts: str | None
    kind: str | None
    scope_id: str | None
    stage: str | None
    model: str | None
    duration_ms: int | None
    status: str | None
    path: str | None


class DebugLogList(BaseModel):
    rows: list[DebugLogRow]
    log_root: str


class DebugLogView(BaseModel):
    path: str
    body: str

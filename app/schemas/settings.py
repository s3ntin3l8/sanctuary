"""Settings tabs: account, Gmail, identity, AI & models, appearance, data."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.models.enums import UserRole

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


class GmailView(BaseModel):
    connected: bool
    connected_at: str | None
    last_sync_at: str | None
    allowlist: list[str]
    label_filter: str
    oauth_start_url: str


class GmailFilters(BaseModel):
    allowlist: list[str]
    label_filter: str = ""


class GmailBackfill(BaseModel):
    days: Literal[90, 365, 1825]


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

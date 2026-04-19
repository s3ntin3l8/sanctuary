import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app.api import api_router
from app.config import (
    CORS_ORIGINS,
    SCAN_FAILED_DIR,
    SCAN_INCOMING_DIR,
    SCAN_PROCESSED_DIR,
    SCAN_PROCESSING_DIR,
    SessionLocal,
    engine,
    templates,
)
from app.constants import REVIEW_FIELD_LABELS
from app.dependencies import get_db
from app.helpers import format_eur, format_relative_time
from app.models.database import (
    ActionItem,
    Case,
    CaseStatus,
    CostCategory,
    CostStatus,
    LegalCost,
)
from app.models.enums import ActionItemType
from app.services.normalization import normalize_hm

# --- Logging Configuration ---
_log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s | %(request_id)-8s | [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


class RequestIDLogRecord(logging.LogRecord):
    """LogRecord with default request_id."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not hasattr(self, "request_id"):
            self.request_id = "-"


class RequestIDFilter(logging.Filter):
    """Add request_id to log records."""

    def filter(self, record):
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


logging.setLogRecordFactory(RequestIDLogRecord)
logging.getLogger().addFilter(RequestIDFilter())


# --- Rate Limiter ---
limiter = Limiter(key_func=get_remote_address, default_limits=["20/minute"])


# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    import sqlite3

    from alembic import command
    from alembic.config import Config as _AlembicConfig

    db_path = str(engine.url).replace("sqlite:///", "")
    needs_migration = False
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("SELECT version_num FROM alembic_version")
        row = cursor.fetchone()
        conn.close()
        if row is None:
            needs_migration = True
    except (sqlite3.OperationalError, Exception):
        needs_migration = True

    if needs_migration:
        alembic_cfg = _AlembicConfig("alembic.ini")
        command.upgrade(alembic_cfg, "head")

    for scan_dir in (
        SCAN_INCOMING_DIR,
        SCAN_PROCESSING_DIR,
        SCAN_PROCESSED_DIR,
        SCAN_FAILED_DIR,
    ):
        scan_dir.mkdir(parents=True, exist_ok=True)

    db: Session = SessionLocal()
    try:
        for seed in _SEED_CASES:
            if not db.get(Case, seed["id"]):
                db.add(Case(**seed))
        db.commit()

        if (
            db.query(ActionItem)
            .filter(ActionItem.case_id.in_([s["id"] for s in _SEED_CASES]))
            .count()
            == 0
        ):
            now = datetime.now(UTC).replace(second=0, microsecond=0)
            for seed in _SEED_DEADLINES:
                db.add(
                    ActionItem(
                        case_id=seed["case_id"],
                        title=seed["title"],
                        description=seed["description"],
                        due_date=now + timedelta(days=seed["offset_days"]),
                        action_type=ActionItemType.DEADLINE,
                    )
                )

            base_time = datetime.now(UTC).replace(second=0, microsecond=0)
            for seed in _SEED_HEARINGS:
                scheduled_day = base_time + timedelta(days=seed["offset_days"])
                db.add(
                    ActionItem(
                        case_id=seed["case_id"],
                        title=seed["title"],
                        description=seed["description"],
                        location=seed["location"],
                        due_date=scheduled_day.replace(
                            hour=seed["hour"],
                            minute=seed["minute"],
                        ),
                        action_type=ActionItemType.COURT_DATE,
                    )
                )

        if (
            db.query(LegalCost)
            .filter(LegalCost.case_id.in_([s["id"] for s in _SEED_CASES]))
            .count()
            == 0
        ):
            now = datetime.now(UTC).replace(second=0, microsecond=0)

            def _offset_date(offset):
                return now + timedelta(days=offset) if offset is not None else None

            for seed in _SEED_COSTS:
                db.add(
                    LegalCost(
                        case_id=seed["case_id"],
                        category=CostCategory(seed["category"]),
                        status=CostStatus(seed["status"]),
                        title=seed["title"],
                        rvg_position=seed.get("rvg_position"),
                        amount_net=seed["amount_net"],
                        vat_rate=seed["vat_rate"],
                        amount_gross=seed["amount_gross"],
                        amount_paid=seed["amount_paid"],
                        amount_reimbursed=seed.get("amount_reimbursed", 0.0),
                        streitwert=seed.get("streitwert"),
                        gebuehren_faktor=seed.get("gebuehren_faktor"),
                        is_reimbursable=seed.get("is_reimbursable", True),
                        notes=seed.get("notes"),
                        issued_at=_offset_date(seed.get("offset_issued")),
                        due_at=_offset_date(seed.get("offset_due")),
                        paid_at=_offset_date(seed.get("offset_paid")),
                    )
                )

        db.commit()
    finally:
        db.close()
    yield


async def add_request_id(request: Request, call_next):
    """Add unique request ID to each request and log request lifecycle."""
    request_id = str(uuid4())[:8]
    request.state.request_id = request_id

    logger.info(f"Request started: {request.method} {request.url.path}")

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id

    logger.info(
        f"Request completed: {request.method} {request.url.path} -> {response.status_code}"
    )

    return response


# --- FastAPI App ---
app = FastAPI(
    title="The Sanctuary",
    description="Privacy-first legal case management.",
    version="1.0.0",
    lifespan=lifespan,
)

# Compression middleware (outermost - processes responses first)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.limiter = limiter

app.middleware("http")(add_request_id)

# Mount static files early
PROJECT_ROOT = Path(__file__).parent.parent
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(PROJECT_ROOT / "static" / "favicon.png")


# -- Seed Data ---
_SEED_CASES = [
    {
        "id": "_TRIAGE",
        "title": "Triage Inbox",
        "status": CaseStatus.INTAKE,
    },
    {
        "id": "ADV-992-K",
        "title": "Vane vs. Vane: Divorce & Assets",
        "status": CaseStatus.DISCOVERY,
    },
    {
        "id": "ADV-804-M",
        "title": "Smith Construction vs. City Council",
        "status": CaseStatus.PRE_TRIAL,
    },
    {
        "id": "REF-441-22",
        "title": "Mercury Tech IP Dispute",
        "status": CaseStatus.CLOSED,
    },
]

_SEED_DEADLINES = [
    {
        "case_id": "ADV-992-K",
        "title": "File supplemental financial disclosure",
        "description": "Updated asset schedule requested before the next conference.",
        "offset_days": 3,
    },
    {
        "case_id": "ADV-804-M",
        "title": "Respond to interrogatories",
        "description": "Serve final discovery responses on opposing counsel.",
        "offset_days": 6,
    },
    {
        "case_id": "ADV-804-M",
        "title": "Submit witness exhibit list",
        "description": "Court requires pre-trial exhibit exchange before motion hearing.",
        "offset_days": 11,
    },
]

_SEED_HEARINGS = [
    {
        "case_id": "ADV-992-K",
        "title": "Settlement conference",
        "description": "Case management conference with both parties present.",
        "location": "Superior Court, Room 4B",
        "offset_days": 5,
        "hour": 9,
        "minute": 30,
    },
    {
        "case_id": "ADV-804-M",
        "title": "Pre-trial motions hearing",
        "description": "Argument on municipal records and expert disclosure motions.",
        "location": "City Hall, Hearing Room C",
        "offset_days": 10,
        "hour": 14,
        "minute": 0,
    },
]

_SEED_COSTS = [
    {
        "case_id": "ADV-992-K",
        "category": CostCategory.ANWALTSKOSTEN,
        "status": CostStatus.BEZAHLT,
        "title": "Retainer for Discovery Phase",
        "amount_net": 5000.0,
        "vat_rate": 0.19,
        "amount_gross": 5950.0,
        "amount_paid": 5950.0,
        "offset_issued": -30,
        "offset_due": -15,
        "offset_paid": -20,
    },
    {
        "case_id": "ADV-804-M",
        "category": CostCategory.GERICHTSKOSTEN,
        "status": CostStatus.OFFEN,
        "title": "Court Filing Fee",
        "amount_net": 1200.0,
        "vat_rate": 0.0,
        "amount_gross": 1200.0,
        "amount_paid": 0.0,
        "offset_issued": -5,
        "offset_due": 10,
        "offset_paid": None,
    },
]


@app.get("/health")
async def health_check():
    """Lightweight endpoint for Docker health checks."""
    return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}


@app.get("/")
async def root_page(request: Request, db: Session = Depends(get_db)):
    """Root page - serves the dashboard."""
    from app.api.dashboard import dashboard

    return await dashboard(request, db)


templates.env.globals["review_field_labels"] = REVIEW_FIELD_LABELS
templates.env.filters["hm"] = normalize_hm
templates.env.globals["format_eur"] = format_eur
templates.env.filters["format_relative_time"] = format_relative_time
templates.env.filters["urlencode"] = quote

# Markdown renderer.
# html=False blocks raw-HTML passthrough — Docling-produced markdown can't inject
# <script> even if the source PDF was adversarial. typographer upgrades straight
# quotes / dashes; linkify auto-links bare URLs. Tables enabled for Schriftsatz
# cost tables.
from markdown_it import MarkdownIt
from markupsafe import Markup
from markupsafe import escape as _escape

_md = (
    MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
    .enable("table")
    .enable("strikethrough")
)


def render_markdown(value: str | None) -> Markup:
    if not value:
        return Markup("")
    return Markup(_md.render(str(value)))


def render_highlighted(value: str | None, key_passages: list | None = None) -> Markup:
    """Render markdown then wrap key_passage text in slate-blue <mark> spans.

    key_passages is a list of {text, rationale, span} dicts from Document.key_passages.
    No-ops gracefully when the list is empty or None (pre-Phase 4).
    """
    import re as _re

    html = _md.render(str(value)) if value else ""
    if key_passages:
        for passage in key_passages:
            text = (passage.get("text") or "").strip()
            if not text:
                continue
            # Escape for regex, then replace first occurrence preserving case.
            pattern = _re.escape(text)
            rationale = _escape(passage.get("rationale", ""))
            replacement = f'<mark class="bg-sky-800/30 text-sky-200 rounded px-0.5" title="{rationale}">{text}</mark>'
            html = _re.sub(pattern, replacement, html, count=1)
    return Markup(html)


# Filter name kept for backward compat with existing templates.
templates.env.filters["safe_markdown"] = render_markdown
templates.env.filters["render_highlighted"] = render_highlighted

# Rate limiter setup
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Error page defaults
DEFAULT_SIDEBAR_COUNTS = {
    "triage_count": 0,
    "notification_count": 0,
    "pending_count": 0,
    "case_count": 0,
    "cost_count": 0,
}


async def not_found_handler(request: Request, exc: Exception) -> HTMLResponse:
    """Render custom 404 page."""
    return templates.TemplateResponse(
        request,
        "errors/404.html",
        {
            "message": str(exc.detail) if hasattr(exc, "detail") else "Page not found",
            "sidebar_counts": DEFAULT_SIDEBAR_COUNTS,
        },
        status_code=404,
    )


async def server_error_handler(request: Request, exc: Exception) -> HTMLResponse:
    """Render custom 500 page with logging."""
    logger = logging.getLogger(__name__)
    error_msg = str(exc.detail) if hasattr(exc, "detail") else str(exc)
    logger.error(f"Server error on {request.url.path}: {error_msg}", exc_info=True)
    return templates.TemplateResponse(
        request,
        "errors/500.html",
        {
            "message": "An unexpected error occurred.",
            "sidebar_counts": DEFAULT_SIDEBAR_COUNTS,
        },
        status_code=500,
    )


async def validation_error_handler(request: Request, exc: Exception) -> HTMLResponse:
    """Render custom 422 page."""
    return templates.TemplateResponse(
        request,
        "errors/422.html",
        {
            "message": str(exc.detail)
            if hasattr(exc, "detail")
            else "Validation error",
            "sidebar_counts": DEFAULT_SIDEBAR_COUNTS,
        },
        status_code=422,
    )


# Register exception handlers
app.add_exception_handler(404, not_found_handler)
app.add_exception_handler(500, server_error_handler)
app.add_exception_handler(422, validation_error_handler)

app.include_router(api_router)

from app.api import (
    cases,
    contacts,
    costs_router,
    dashboard_router,
    documents_router,
    entities,
    ingestion_settings,
    search,
    triage_router,
)
from app.api.claims import router as claims_router
from app.api.slicing import router as slicing_router

app.include_router(claims_router)
app.include_router(dashboard_router)
app.include_router(triage_router)
app.include_router(slicing_router)
app.include_router(costs_router)
app.include_router(documents_router)
app.include_router(cases.router)
app.include_router(contacts.router)
app.include_router(entities.router)
app.include_router(search.router)
app.include_router(ingestion_settings.router)


if __name__ == "__main__":
    import uvicorn

    from app.config import DEBUG, HOST, PORT

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=DEBUG)

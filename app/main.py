import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.api import api_router
from app.config import (
    CORS_ORIGINS,
    SCAN_FAILED_DIR,
    SCAN_INCOMING_DIR,
    SCAN_PROCESSED_DIR,
    SCAN_PROCESSING_DIR,
    templates,
)
from app.constants import REVIEW_FIELD_LABELS
from app.helpers import format_due_relative, format_eur, format_relative_time
from app.services.normalization import normalize_hm


# --- Logging Configuration ---
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


def setup_logging():
    """Configure robust logging by hijacking third-party loggers."""
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level_str, logging.INFO)

    # Use our custom LogRecord factory globally
    logging.setLogRecordFactory(RequestIDLogRecord)

    # Reconfigure the root logger
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s | %(request_id)-8s | [%(levelname)s] %(name)s: %(message)s"
    )
    handler.setFormatter(formatter)
    handler.addFilter(RequestIDFilter())
    root.addHandler(handler)
    root.setLevel(level)

    # Hijack all existing loggers to propagate to root and use our format
    for name in logging.root.manager.loggerDict:
        target = logging.getLogger(name)
        target.handlers = []
        target.propagate = True

        # Suppress SQLAlchemy INFO noise (like executed queries) by default
        if name.startswith("sqlalchemy") and level != logging.DEBUG:
            target.setLevel(logging.WARNING)
        else:
            target.setLevel(level)


setup_logging()
logger = logging.getLogger(__name__)
logger.info("Logging initialized.")


# --- Rate Limiter ---
limiter = Limiter(key_func=get_remote_address, default_limits=["20/minute"])


# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    for scan_dir in (
        SCAN_INCOMING_DIR,
        SCAN_PROCESSING_DIR,
        SCAN_PROCESSED_DIR,
        SCAN_FAILED_DIR,
    ):
        scan_dir.mkdir(parents=True, exist_ok=True)

    # Run migrations so the schema exists even on a fresh/deleted DB.
    from alembic import command
    from alembic.config import Config as AlembicConfig

    alembic_cfg = AlembicConfig(str(Path(__file__).parent.parent / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")

    # Re-setup logging after alembic might have re-configured it
    setup_logging()
    logging.getLogger(__name__).info("Migrations complete, logging re-verified.")

    yield


async def add_request_id(request: Request, call_next):
    """Add unique request ID to each request and log request lifecycle."""
    request_id = str(uuid4())[:8]
    request.state.request_id = request_id

    logger.info(f"→ {request.method} {request.url.path}")

    try:
        response = await call_next(request)
    except Exception:
        logger.exception(f"Unhandled exception on {request.method} {request.url.path}")
        raise

    response.headers["X-Request-ID"] = request_id

    status = response.status_code
    level = logging.WARNING if status >= 400 else logging.INFO
    logger.log(level, f"← {status} {request.method} {request.url.path}")

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


@app.get("/health")
async def health_check():
    """Lightweight endpoint for Docker health checks."""
    return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}


import hashlib


def _hash_id(text: str, kind: str = "neutral", length: int = 12) -> str:
    return hashlib.sha1(f"{text}|{kind}".encode()).hexdigest()[:length]


templates.env.globals["review_field_labels"] = REVIEW_FIELD_LABELS
templates.env.filters["hm"] = normalize_hm
templates.env.filters["hash"] = _hash_id
templates.env.globals["format_eur"] = format_eur
templates.env.filters["format_relative_time"] = format_relative_time
templates.env.filters["format_due_relative"] = format_due_relative
templates.env.filters["urlencode"] = quote

# Markdown renderer.
# html=False blocks raw-HTML passthrough — Docling-produced markdown can't inject
# <script> even if the source PDF was adversarial. typographer upgrades straight
# quotes / dashes; linkify auto-links bare URLs. Tables enabled for Schriftsatz
# cost tables.
from markdown_it import MarkdownIt
from markupsafe import Markup

_md = (
    MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
    .enable("table")
    .enable("strikethrough")
)


def render_markdown(value: str | None) -> Markup:
    if not value:
        return Markup("")
    return Markup(_md.render(str(value)))


def render_highlighted(
    value: str | None,
    key_passages: list | None = None,
    passage_claim_ids: dict | None = None,
) -> Markup:
    """Render markdown then wrap key_passage text in semantic <mark> spans.

    key_passages is a list of {text, rationale, kind?, id?, start_offset?, end_offset?} dicts.
    passage_claim_ids maps passage_id → claim_id for the ⚖ chip.
    No-ops gracefully when the list is empty or None (pre-Phase 4).

    When start_offset/end_offset are available, the exact source text slice is
    used as the search pattern (avoids AI paraphrasing drift).  Falls back to
    passage["text"] when offsets are missing or invalid.
    """
    import hashlib as _hl
    import re as _re

    raw = str(value) if value else ""
    html = _md.render(raw)
    if not key_passages:
        return Markup(html)

    for passage in key_passages:
        text = (passage.get("text") or "").strip()
        if not text:
            continue
        kind = (passage.get("kind") or "neutral").lower()
        pid = passage.get("id") or _hl.sha1(f"{text}|{kind}".encode()).hexdigest()[:12]

        claim_anchor = ""
        chip = ""
        if passage_claim_ids and pid in passage_claim_ids:
            claim_id = passage_claim_ids[pid]
            chip = f'<a href="#claim-{claim_id}" class="hud-claim-chip ml-0.5 text-[10px] no-underline">⚖</a>'
            claim_anchor = f'<span id="claim-{claim_id}" class="claim-anchor" aria-hidden="true"></span>'
        mark_open = (
            f'{claim_anchor}<mark id="p-{pid}" data-passage-id="{pid}" data-kind="{kind}" '
            f'class="hud-mark hud-mark--{kind} '
            f"bg-[color:var(--color-key-passage-bg)] text-[color:var(--color-key-passage-fg)] "
            f'rounded px-0.5 ring-1 ring-[color:var(--color-key-passage-ring)]">'
        )
        mark_close = f"</mark>{chip}"

        # Prefer exact source text from validated offsets over AI-quoted text.
        start = passage.get("start_offset")
        end = passage.get("end_offset")
        search_text = text
        if (
            start is not None
            and end is not None
            and isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start < end <= len(raw)
        ):
            search_text = raw[start:end]

        def _replace(m, mo=mark_open, mc=mark_close):
            return mo + m.group(0) + mc

        new_html = _re.sub(_re.escape(search_text), _replace, html, count=1)
        if new_html == html and search_text != text:
            # Offset-derived text didn't survive markdown rendering; fall back.
            new_html = _re.sub(_re.escape(text), _replace, html, count=1)
        if new_html == html:
            # Neither attempt matched (markdown escaping / AI paraphrase).
            # Inject a hidden anchor at the start so clicks scroll to the top
            # of the body — better than a no-op.
            new_html = (
                f'<a id="p-{pid}" class="passage-anchor-unmatched" aria-hidden="true"></a>'
                + html
            )
        html = new_html

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
    documents_router,
    home_router,
    ingestion_settings,
    search,
    triage_router,
)
from app.api.chat import router as chat_router
from app.api.claims import router as claims_router
from app.api.settings_ai_config import router as settings_ai_router
from app.api.settings_appearance import router as settings_appearance_router
from app.api.settings_maintenance import router as settings_maintenance_router
from app.api.settings_page import router as settings_page_router
from app.api.slicing import router as slicing_router
from app.api.user_settings import router as user_settings_router

app.include_router(chat_router)
app.include_router(user_settings_router)
app.include_router(claims_router)
app.include_router(home_router)
app.include_router(triage_router)
app.include_router(slicing_router)
app.include_router(costs_router)
app.include_router(documents_router)
app.include_router(cases.router)
app.include_router(contacts.router)
app.include_router(search.router)
app.include_router(ingestion_settings.router)
app.include_router(settings_page_router)
app.include_router(settings_ai_router)
app.include_router(settings_appearance_router)
app.include_router(settings_maintenance_router)


if __name__ == "__main__":
    import uvicorn

    from app.config import DEBUG, HOST, PORT

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=DEBUG)

import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
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
from slowapi.middleware import SlowAPIMiddleware
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


class SuccessfulAccessFilter(logging.Filter):
    """Downgrade uvicorn.access records for 2xx/3xx responses to DEBUG.

    At INFO log level the handler suppresses DEBUG records, so polling
    traffic disappears. At DEBUG log level every request is still visible.
    4xx/5xx stay at INFO so real failures always surface.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access uses positional args: (client, request_line, status)
        status = None
        if isinstance(record.args, tuple) and len(record.args) >= 5:
            status = record.args[-1]
        else:
            status = getattr(record, "status_code", None)
        try:
            if status is not None and 200 <= int(status) < 400:
                record.levelno = logging.DEBUG
                record.levelname = "DEBUG"
        except (TypeError, ValueError):
            pass
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

    formatter = logging.Formatter(
        "%(asctime)s | %(request_id)-8s | [%(levelname)s] %(name)s: %(message)s"
    )

    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(RequestIDFilter())
    root.addHandler(console_handler)

    # Rotating File Handler
    log_dir = Path("scratch")
    log_dir.mkdir(exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "sanctuary.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(RequestIDFilter())
    root.addHandler(file_handler)

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

        # Drop successful HTTP access logs from uvicorn at non-DEBUG levels —
        # 2xx/3xx polling traffic dominates the log otherwise. 4xx/5xx still
        # propagate so real failures stay visible.
        if name == "uvicorn.access":
            target.addFilter(SuccessfulAccessFilter())


setup_logging()
logger = logging.getLogger(__name__)
logger.info("Logging initialized.")


# --- Rate Limiter ---
limiter = Limiter(key_func=get_remote_address, default_limits=["600/minute"])


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

    # Skip every production-DB side effect under pytest. The conftest creates
    # the test schema via Base.metadata.create_all on a separate engine, so
    # running migrations / seeding / recovery against the hardcoded
    # alembic.ini URL (sqlite:///data/sanctuary.db) would race with `make run`'s
    # WAL locks and cause hangs or "readonly database" errors.
    if os.getenv("PYTEST_CURRENT_TEST"):
        yield
        return

    # Run migrations so the schema exists even on a fresh/deleted DB.
    from alembic import command
    from alembic.config import Config as AlembicConfig

    alembic_cfg = AlembicConfig(str(Path(__file__).parent.parent / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")

    # Re-setup logging after alembic might have re-configured it
    setup_logging()
    logging.getLogger(__name__).info("Migrations complete, logging re-verified.")

    # Seed singletons that production code paths depend on. Required for FK
    # enforcement (PRAGMA foreign_keys=ON) — any ingest into the triage inbox
    # references case_id="_TRIAGE" and would 500 without this row.
    from app.dependencies import SessionLocal
    from app.services.case_service import seed_triage_case

    with SessionLocal() as seed_db:
        seed_triage_case(seed_db)

    # Reset any pipeline stages that were left in RUNNING state by a prior crash.
    from app.services.pipeline_status import (
        recover_orphaned_running_stages,
        recover_stuck_pending_dispatches,
    )

    with SessionLocal() as recovery_db:
        stats = recover_orphaned_running_stages(recovery_db)
    if any(stats.values()):
        logging.getLogger(__name__).warning("Pipeline recovery on startup: %s", stats)

    # Re-dispatch docs whose process_document_task daemon thread was killed by
    # uvicorn --reload before it could call mark_started (EAGER mode hazard).
    with SessionLocal() as pending_db:
        pending_stats = recover_stuck_pending_dispatches(pending_db)
    if pending_stats.get("docs_redispatched"):
        logging.getLogger(__name__).warning(
            "Pipeline recovery on startup (stuck pending): %s", pending_stats
        )

    # vec0 cannot be ALTERed: if the active embed instance's dim diverges from the
    # document_vectors schema, every embedding write fails the per-row dim guard.
    # Two cases:
    #   (a) stored dim is missing/unset (legacy, pre-auto-detect) → schema is the ground
    #       truth; sync it into the instance so the UI and per-write guard agree.
    #   (b) stored dim was explicitly set but differs from schema → user changed embed
    #       model without rebuilding; log a warning so they know to rebuild.
    from app.services.ai_config import (
        _ensure_migrated,
        _get_ai_section,
        get_instance,
        save_instance,
    )
    from app.services.embeddings import verify_vec0_dim

    with SessionLocal() as vec_db:
        _ensure_migrated(vec_db)
        ai = _get_ai_section(vec_db)
        active_id = ai.get("active_embed_id")
        inst = get_instance(vec_db, active_id) if active_id else None
        stored_dim = inst.get("embed_dim") if inst else None  # None = never probed
        ok, actual = verify_vec0_dim(vec_db, stored_dim or 0)
        if actual is not None and stored_dim != actual:
            if not stored_dim and inst is not None:
                # Case (a): dim was never stored — sync from schema silently.
                updated = dict(inst)
                updated["embed_dim"] = actual
                save_instance(vec_db, updated)
                logging.getLogger(__name__).info(
                    "Startup: set active embed instance dim to %s from document_vectors schema.",
                    actual,
                )
            elif stored_dim:
                # Case (b): explicit dim mismatch — user needs to rebuild.
                logging.getLogger(__name__).error(
                    "embed_dim=%s (active embed instance) but document_vectors schema "
                    "declares dim=%s. vec0 can't be ALTERed — use Settings → AI → "
                    "Rebuild Index to recreate it.",
                    stored_dim,
                    actual,
                )

    yield


async def add_request_id(request: Request, call_next):
    """Add unique request ID to each request and log request lifecycle."""
    request_id = str(uuid4())[:8]
    request.state.request_id = request_id

    logger.debug(f"→ {request.method} {request.url.path}")

    try:
        response = await call_next(request)
    except Exception:
        logger.exception(f"Unhandled exception on {request.method} {request.url.path}")
        raise

    response.headers["X-Request-ID"] = request_id

    status = response.status_code
    if status >= 500:
        level = logging.ERROR
    elif status >= 400:
        level = logging.WARNING
    else:
        level = logging.DEBUG
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
app.add_middleware(SlowAPIMiddleware)

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
import re


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


# Highlight sentinels — private-use unicode pairs that survive markdown
# rendering as opaque text and are easy to swap for <mark> tags afterwards.
# Earlier implementations regex-matched AI-quoted text against the rendered
# HTML, which broke whenever markdown escaping (smart quotes, dashes, &<>),
# inline formatting (**bold**, _em_), or paragraph wrapping rewrote the text.
# Splicing into the *raw* markdown sidesteps all of that: offsets are stamped
# at ingest time against doc.content, and sentinels travel with their text
# through the renderer.
_HL_SENT_BEGIN = "HLB{:04d}"
_HL_SENT_END = "HLE{:04d}"
_HL_SENT_PAIR_RE = re.compile(r"HLB(\d{4})(.*?)HLE\1", re.DOTALL)
_HL_SENT_ORPHAN_RE = re.compile(r"HL[BE]\d{4}")


def render_highlighted(
    value: str | None,
    key_passages: list | None = None,
    passage_claim_ids: dict | None = None,
    claim_excerpt_map: dict | None = None,
) -> Markup:
    """Render markdown with key_passages and claim excerpts wrapped in <mark>.

    For each highlight, sentinel pairs are spliced into the *raw* markdown at
    validated offsets, then markdown-it renders the whole thing, then a regex
    swap replaces sentinel pairs with <mark> tags. Highlights that can't be
    located fall back to a hidden anchor at the top of the body so spine
    clicks still have a target.
    """
    from app.services.text_offsets import find_text_offsets

    raw = str(value) if value else ""
    if not raw:
        return Markup("")
    if not key_passages and not claim_excerpt_map:
        return Markup(_md.render(raw))

    highlights: list[dict] = []  # {start, end, open, close}
    fallback_anchors: list[str] = []  # injected at top when match fails

    # ── Key passages ──────────────────────────────────────────────────────
    for passage in key_passages or []:
        text = (passage.get("text") or "").strip()
        if not text:
            continue
        kind = (passage.get("kind") or "neutral").lower()
        pid = (
            passage.get("id")
            or hashlib.sha1(f"{text}|{kind}".encode()).hexdigest()[:12]
        )

        start = passage.get("start_offset")
        end = passage.get("end_offset")
        valid = (
            isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start < end <= len(raw)
        )
        if not valid:
            offsets = find_text_offsets(raw, text)
            if offsets:
                start, end = offsets
                valid = True

        claim_id = (passage_claim_ids or {}).get(pid)
        claim_anchor = (
            f'<span id="claim-{claim_id}" class="claim-anchor" aria-hidden="true"></span>'
            if claim_id
            else ""
        )
        chip = (
            f'<a href="#claim-{claim_id}" class="hud-claim-chip ml-0.5 text-[10px] no-underline">⚖</a>'
            if claim_id
            else ""
        )

        if not valid:
            # No reliable position — leave a hidden anchor at the top so
            # spine clicks still resolve to *something*. The spine row already
            # surfaces "⚠ approx" for these.
            fallback_anchors.append(
                f'{claim_anchor}<a id="p-{pid}" class="passage-anchor-unmatched" aria-hidden="true"></a>'
            )
            continue

        mark_open = (
            f'{claim_anchor}<mark id="p-{pid}" data-passage-id="{pid}" data-kind="{kind}" '
            f'class="hud-mark hud-mark--{kind} '
            f"bg-[color:var(--color-key-passage-bg)] text-[color:var(--color-key-passage-fg)] "
            f'rounded px-0.5 ring-1 ring-[color:var(--color-key-passage-ring)]">'
        )
        mark_close = f"</mark>{chip}"
        highlights.append(
            {"start": start, "end": end, "open": mark_open, "close": mark_close}
        )

    # ── Independent claim excerpts (amber) ───────────────────────────────
    for claim_id, excerpt in (claim_excerpt_map or {}).items():
        if not excerpt:
            continue
        offsets = find_text_offsets(raw, excerpt)
        if not offsets:
            fallback_anchors.append(
                f'<a id="claim-{claim_id}" class="claim-anchor-unmatched" aria-hidden="true"></a>'
            )
            continue
        start, end = offsets
        mark_open = (
            f'<mark id="claim-{claim_id}" data-claim-id="{claim_id}" '
            f'class="hud-mark hud-mark--claim '
            f"bg-[color:var(--color-claim-bg)] text-[color:var(--color-claim-fg)] "
            f'rounded px-0.5 ring-1 ring-[color:var(--color-claim-ring)]">'
        )
        highlights.append(
            {"start": start, "end": end, "open": mark_open, "close": "</mark>"}
        )

    if not highlights:
        html = _md.render(raw)
        if fallback_anchors:
            html = "".join(fallback_anchors) + html
        return Markup(html)

    # Splice sentinels into raw at every (start, end) pair. We assemble events
    # and walk left-to-right so overlapping/nested ranges all line up against
    # the original offsets — splicing in reverse over a mutating string would
    # mis-align the second range when ranges nest.
    events: list[tuple[int, int, int, str]] = []
    # tuple: (position, priority, idx, sentinel_text)
    # priority: 0 = end-tag (so an end at pos N closes before another's start at N),
    #           1 = start-tag.
    for idx, h in enumerate(highlights):
        events.append((h["start"], 1, idx, _HL_SENT_BEGIN.format(idx)))
        events.append((h["end"], 0, idx, _HL_SENT_END.format(idx)))
    events.sort(key=lambda e: (e[0], e[1]))

    pieces: list[str] = []
    cursor = 0
    for pos, _, _, sentinel in events:
        if pos > cursor:
            pieces.append(raw[cursor:pos])
        pieces.append(sentinel)
        cursor = pos
    pieces.append(raw[cursor:])
    spliced = "".join(pieces)

    html = _md.render(spliced)

    sentinel_to_html = {
        idx: (h["open"], h["close"]) for idx, h in enumerate(highlights)
    }

    def _swap(match: re.Match) -> str:
        idx = int(match.group(1))
        body = match.group(2)
        open_html, close_html = sentinel_to_html.get(idx, ("", ""))
        return f"{open_html}{body}{close_html}"

    # Apply repeatedly so sentinel pairs nested inside another pair's body
    # also get swapped (regex captures the outer pair first, then we re-scan
    # the result). Bound the loop to avoid pathological inputs.
    for _ in range(8):
        new_html = _HL_SENT_PAIR_RE.sub(_swap, html)
        if new_html == html:
            break
        html = new_html

    # Strip any orphan sentinels (e.g. crossing ranges where one half lost
    # its mate during regex consumption). Better to render text cleanly than
    # leak  control glyphs.
    html = _HL_SENT_ORPHAN_RE.sub("", html)

    if fallback_anchors:
        html = "".join(fallback_anchors) + html

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
    "pipeline_active_count": 0,
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

from app.api.worker_queue import router as worker_queue_router

app.include_router(worker_queue_router)


if __name__ == "__main__":
    import uvicorn

    from app.config import DEBUG, HOST, PORT

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=DEBUG)

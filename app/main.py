import base64
import hashlib
import hmac
import html
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler as fastapi_http_exception_handler,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.errors import (
    error_response,
    http_error_response,
    is_api_path,
    validation_error_response,
)
from app.config import (
    CORS_ORIGINS,
    DEBUG,
    FRONTEND_DIST,
    SCAN_FAILED_DIR,
    SCAN_INCOMING_DIR,
    SCAN_PROCESSED_DIR,
    SCAN_PROCESSING_DIR,
)
from app.core.log_formatter import LocalTimeFormatter
from app.core.rate_limit import limiter
from app.core.secrets import SecretsError
from app.spa import ImmutableStaticFiles, spa_index


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
            # status's real type is whatever uvicorn/logging puts in record.args
            # or record.status_code — inherently untyped upstream. The except
            # below already handles anything int() can't convert.
            if status is not None and 200 <= int(status) < 400:  # type: ignore[call-overload]
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

    formatter = LocalTimeFormatter(
        "%(asctime)s | %(request_id)-8s | [%(levelname)s] %(name)s: %(message)s"
    )

    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(RequestIDFilter())
    root.addHandler(console_handler)

    # Rotating File Handler — disabled when SANCTUARY_LOG_FILE=0 (e.g. test suite)
    if os.getenv("SANCTUARY_LOG_FILE", "1") != "0":
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
    root.disabled = False

    # Hijack all existing loggers to propagate to root and use our format
    for name in logging.root.manager.loggerDict:
        target = logging.getLogger(name)
        target.handlers = []
        target.propagate = True
        # `disabled` is separate from `level`/`propagate` and isn't reset by
        # either. Found via #98: this module's own lifespan re-runs alembic
        # migrations in-process (see run_migrations below), and alembic's
        # env.py calls `logging.config.fileConfig(alembic.ini)`, which
        # defaults to disable_existing_loggers=True -- silently killing
        # every logger already registered at that point (app.main,
        # app.access, ...) that alembic.ini's own [loggers] section doesn't
        # list. This function is called again right after migrations
        # specifically to recover from that (see "logging re-verified"
        # below), but without this line it only reset level/handlers/
        # propagate, leaving `.disabled` permanently True -- so every
        # app.main-based logger (add_request_id, server_error_handler,
        # AccessLogMiddleware) silently dropped every message for the rest
        # of the process's life, which is why #98's 500s produced zero log
        # output despite multiple rounds of added instrumentation.
        target.disabled = False

        # Suppress noisy INFO-only libraries at non-DEBUG levels
        if (
            name.startswith("sqlalchemy") or name == "httpx"
        ) and level != logging.DEBUG:
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


_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SESSION_COOKIE = "session"


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin:
        # Compare host only, not scheme. Behind a TLS-terminating reverse proxy
        # that doesn't forward X-Forwarded-Proto, request.url.scheme is "http"
        # while the browser's Origin is "https://…", which would 403 every
        # mutation. The Host match is the meaningful CSRF boundary here (an
        # attacker can't serve content from the same host); the session cookie
        # is already SameSite=lax. Origin has no path, so netloc == host[:port].
        origin_host = origin.split("://", 1)[-1]
        return origin_host == request.headers.get("host", "")

    # No Origin header. Modern browsers send Origin on every cross-origin
    # mutation, so this is usually a same-origin request. Tighten with the
    # Sec-Fetch-Site fetch metadata when present — `cross-site` / `same-site`
    # are explicit cross-origin signals; anything else (`same-origin`,
    # `none`, missing) is treated as same-origin.
    fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
    return fetch_site not in {"cross-site", "same-site"}


def _session_secret() -> bytes:
    return os.getenv(
        "SESSION_SECRET", os.getenv("SECRET_KEY", "dev-session-key")
    ).encode()


def _load_session_cookie(raw: str | None) -> dict:
    if not raw or "." not in raw:
        return {}
    payload_b64, sig_b64 = raw.rsplit(".", 1)
    expected = hmac.new(
        _session_secret(), payload_b64.encode(), hashlib.sha256
    ).digest()
    try:
        actual = base64.urlsafe_b64decode(sig_b64.encode())
    except Exception:
        return {}
    if not hmac.compare_digest(expected, actual):
        return {}
    try:
        payload = base64.urlsafe_b64decode(payload_b64.encode())
        data = json.loads(payload.decode())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _dump_session_cookie(data: dict) -> str:
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    payload_b64 = base64.urlsafe_b64encode(payload).decode()
    sig = hmac.new(_session_secret(), payload_b64.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode()
    return f"{payload_b64}.{sig_b64}"


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
    # the test schema via Base.metadata.create_all on a separate test database,
    # so running migrations / seeding / recovery against the dev DATABASE_URL
    # here would race with a concurrently running `make run` / worker.
    if os.getenv("PYTEST_CURRENT_TEST"):
        yield
        return

    from app.config import AUTH_ENABLED as _AUTH_ENABLED
    from app.config import HOST as _HOST

    _LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
    _is_loopback = _HOST in _LOOPBACK_HOSTS
    _is_default_secret = _session_secret() == b"dev-session-key"

    # With authentication on, the signed session cookie carries the user's
    # identity. A default (publicly-known) secret means anyone can forge a
    # cookie claiming uid=admin — so require a real secret unconditionally,
    # even on loopback / in DEBUG.
    if _AUTH_ENABLED and _is_default_secret:
        raise RuntimeError(
            "SESSION_SECRET is unset while AUTH_ENABLED=true. A default secret "
            "lets anyone forge an admin session cookie. Set SESSION_SECRET in .env "
            "(generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))'), "
            "or set AUTH_ENABLED=false for single-user dev mode."
        )

    if not DEBUG and _is_default_secret:
        raise RuntimeError(
            "SESSION_SECRET is unset and DEBUG=False. "
            "Set SESSION_SECRET in .env "
            "(generate with: python -c 'import secrets; print(secrets.token_urlsafe(32))')."
        )

    # Even with auth disabled, a non-loopback bind on a default secret exposes
    # every endpoint to the network. Refuse to start.
    if not _is_loopback and _is_default_secret:
        raise RuntimeError(
            f"HOST={_HOST!r} is not loopback and SESSION_SECRET is unset. "
            "Either set HOST=127.0.0.1 or set SESSION_SECRET in .env."
        )

    if not _is_loopback and not _AUTH_ENABLED:
        logging.getLogger(__name__).warning(
            "HOST=%s is not loopback and AUTH_ENABLED=false; "
            "anyone reachable at that address can read and mutate case data.",
            _HOST,
        )

    # Run migrations so the schema exists even on a fresh/deleted DB.
    from alembic import command
    from alembic.config import Config as AlembicConfig

    # Tells alembic/env.py this is an in-process call, not a standalone
    # `alembic` CLI invocation, so it skips `fileConfig(alembic.ini)` --
    # that call defaults to disable_existing_loggers=True, which silently
    # kills every logger already registered at this point (app.main,
    # app.access, ...). setup_logging()'s `disabled` reset below is a second
    # line of defense in case this ever gets bypassed (e.g. a bare `alembic`
    # CLI run elsewhere in the same process) -- see its comment for the
    # full mechanism.
    os.environ.setdefault("SANCTUARY_APP", "1")

    alembic_cfg = AlembicConfig(str(Path(__file__).parent.parent / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")

    # Re-setup logging after alembic might have re-configured it
    setup_logging()
    logging.getLogger(__name__).info("Migrations complete, logging re-verified.")

    # Seed singletons that production code paths depend on. Required for FK
    # enforcement — any ingest into the triage inbox references
    # case_id="_TRIAGE" and would 500 without this row.
    from app.dependencies import SessionLocal
    from app.services.case_service import seed_triage_case

    with SessionLocal() as seed_db:
        seed_triage_case(seed_db)

    from app.core.secrets import require_key_if_secrets_stored

    with SessionLocal() as secrets_db:
        require_key_if_secrets_stored(secrets_db)

    # Pin the primary admin (by id) for the worker/dev-mode code paths. Idempotent
    # and safe to call always: it pins an existing admin, optionally seeds one from
    # BOOTSTRAP_ADMIN_EMAIL+PASSWORD on a fresh DB (the code-driven provisioning
    # path), or no-ops on a fresh DB with no env creds — the first-run create-admin
    # screen onboards the first admin in that case.
    from app.services import auth_service

    with SessionLocal() as admin_db:
        auth_service.get_or_create_bootstrap_admin(admin_db)
        admin_db.commit()

    # Reconcile per-user scan-ingest subfolders (incoming/<username>/) and
    # backfill any missing usernames for existing accounts.
    from app.models.database import User as _User
    from app.services import auth_service as _auth_service

    with SessionLocal() as folder_db:
        for _u in folder_db.query(_User).all():
            _auth_service.ensure_username(folder_db, _u)
            _auth_service.ensure_user_scan_dir(_u)
        folder_db.commit()

    # Reset any pipeline stages that were left in RUNNING state by a prior crash.
    from app.services.pipeline_status import (
        recover_orphaned_running_stages,
        recover_stranded_batch_pending,
        recover_stuck_pending_dispatches,
    )

    with SessionLocal() as recovery_db:
        # At startup every RUNNING stage IS an orphan (workers just restarted),
        # so bypass the cron-mode age threshold by setting it to 0. The cron
        # caller in maintenance.recover_pipeline_task uses the default (20 min)
        # to avoid killing legitimately long-running tasks mid-flight.
        stats = recover_orphaned_running_stages(recovery_db, min_age_seconds=0)
    if any(stats.values()):
        logging.getLogger(__name__).warning("Pipeline recovery on startup: %s", stats)

    # Recover docs whose batch_analysis=pending while their batch siblings are
    # done: happens when a single doc's metadata is retried in an already-
    # analyzed batch — the idempotency guard blocks claim_batch_for_analysis.
    # Must run before recover_stuck_pending_dispatches to prevent double-dispatch.
    with SessionLocal() as batch_pending_db:
        batch_pending_stats = recover_stranded_batch_pending(batch_pending_db)
    if batch_pending_stats.get("docs_recovered"):
        logging.getLogger(__name__).warning(
            "Pipeline recovery on startup (stranded batch-pending): %s",
            batch_pending_stats,
        )

    # Re-dispatch docs whose process_document_task daemon thread was killed by
    # uvicorn --reload before it could call mark_started (EAGER mode hazard).
    with SessionLocal() as pending_db:
        pending_stats = recover_stuck_pending_dispatches(pending_db)
    if pending_stats.get("docs_redispatched"):
        logging.getLogger(__name__).warning(
            "Pipeline recovery on startup (stuck pending): %s", pending_stats
        )

    # Changing embed dim requires clearing document_chunks.embedding before the
    # column can be ALTERed to a new vector(N) — existing rows at the old
    # dimension can't coexist with a new declared width. If the active embed
    # instance's dim diverges from the column's declared width, every
    # embedding write fails the per-row dim guard. Two cases:
    #   (a) stored dim is missing/unset (legacy, pre-auto-detect) → schema is the ground
    #       truth; sync it into the instance so the UI and per-write guard agree.
    #   (b) stored dim was explicitly set but differs from schema → user changed embed
    #       model without rebuilding; log a warning so they know to rebuild.
    from app.services.ai_config import (
        _get_ai_section,
        get_instance,
        save_instance,
    )
    from app.services.embeddings import verify_embedding_dim

    with SessionLocal() as vec_db:
        ai = _get_ai_section(vec_db)
        active_id = ai.get("active_embed_id")
        inst = get_instance(vec_db, active_id) if active_id else None
        stored_dim = inst.get("embed_dim") if inst else None  # None = never probed
        ok, actual = verify_embedding_dim(vec_db, stored_dim or 0)
        if actual is not None and stored_dim != actual:
            if not stored_dim and inst is not None:
                # Case (a): dim was never stored — sync from schema silently.
                updated = dict(inst)
                updated["embed_dim"] = actual
                save_instance(vec_db, updated)
                logging.getLogger(__name__).info(
                    "Startup: set active embed instance dim to %s from document_chunks.embedding schema.",
                    actual,
                )
            elif stored_dim:
                # Case (b): explicit dim mismatch — user needs to rebuild.
                logging.getLogger(__name__).error(
                    "embed_dim=%s (active embed instance) but document_chunks.embedding "
                    "declares dim=%s. Use Settings → AI → Rebuild Index to clear and "
                    "re-embed at the new dimension.",
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


class AccessLogMiddleware:
    """Outermost pure-ASGI request/response logger.

    Registered last in the `app.add_middleware(...)` chain below, so it ends
    up outermost (Starlette's `add_middleware` prepends -- the final call
    lands right inside `ServerErrorMiddleware`, ahead of every other layer:
    AuthGate, OriginGuard, `add_request_id`, SlowAPI, CORS, session, gzip,
    and Starlette's own ExceptionMiddleware).

    Exists to close an observability gap found in issue #98: `/triage/confirm`
    intermittently returned a real, app-rendered `errors/500.html` in CI with
    *zero* corresponding log line. Neither `add_request_id` (only logs on a
    raised exception, or once `call_next` returns a response) nor
    `server_error_handler` (only runs when Starlette's ExceptionMiddleware
    actually dispatches to the registered 500 handler) fired -- meaning
    whatever inner layer produced that response bypassed both logging paths.

    This middleware doesn't depend on any inner layer's control flow: it
    reads the response status directly off the raw `http.response.start`
    ASGI message via a `send` wrapper, so it logs a 5xx regardless of *how*
    the inner stack produced it. It also logs (and re-raises) any exception
    that reaches this layer without a response ever being sent -- the true
    outermost safety net, one layer inside `ServerErrorMiddleware`.

    Two follow-up fixes (from a second, still-unexplained #98 recurrence
    *after* this middleware first shipped -- see the issue thread): the
    5xx/4xx line is logged from inside `send_wrapper`, at the instant
    `http.response.start` goes out, not after `self.app(...)` returns --
    a handler that does work after sending the response (e.g. this repo's
    confirm route dispatching a `CELERY_TASK_ALWAYS_EAGER` background task
    right after responding) could otherwise leave the log line unwritten
    for a while, or the process could be killed in that window before it's
    ever written. And the outer guard catches `BaseException`, not just
    `Exception` -- `asyncio.CancelledError` is a `BaseException` and would
    otherwise reach the client's response (or lack thereof) with no log
    line at all.
    """

    def __init__(self, app):
        self.app = app
        self._logger = logging.getLogger("app.access")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "-")
        path = scope.get("path", "-")

        if method in _MUTATING_METHODS:
            # Entry marker for mutating requests only (GETs would flood
            # production logs). Exists to settle a still-open question from
            # #98's second recurrence: even with the send_wrapper fix below
            # and a `BaseException` catch, a confirmed CI 500 produced no
            # `access:`/exception line at all. That means either (a) the
            # request never reached this middleware's self.app(...) call, or
            # (b) it did, but exited some other way (e.g. Starlette's
            # ServerErrorMiddleware, which sits *outside* this middleware).
            # This line's presence or absence in the next occurrence's log
            # settles (a) vs (b) directly.
            self._logger.info("access: entry %s %s", method, path)

        async def send_wrapper(message):
            # Log at the moment the status line goes out, not after
            # self.app(...) returns. Issue #98's 500 was rendered and sent to
            # the client, yet went unlogged: the app's post-response work
            # (this repo's confirm route dispatches a CELERY_TASK_ALWAYS_EAGER
            # background thread right after responding) can occupy the
            # coroutine for a while after `send` has already delivered the
            # response, or CI can kill the process in that window. Logging
            # from inside send_wrapper means the line is written as soon as
            # the response exists, independent of whatever the app does next.
            if message["type"] == "http.response.start":
                status = message["status"]
                if status >= 500:
                    self._logger.error("access: %s %s -> %s", method, path, status)
                elif status >= 400:
                    self._logger.warning("access: %s %s -> %s", method, path, status)
                else:
                    self._logger.debug("access: %s %s -> %s", method, path, status)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException:
            # BaseException, not Exception: asyncio.CancelledError (raised
            # when a request is cancelled under load, e.g. client disconnect
            # or server shutdown mid-request) is a BaseException and would
            # otherwise slip past this guard unlogged and unre-raised-loudly.
            self._logger.error(
                "Unhandled exception reached outermost logger on %s %s "
                "(no response sent)",
                method,
                path,
                exc_info=True,
            )
            raise


class OriginGuardMiddleware:
    """Block cross-origin browser mutations for localhost/no-auth deployments."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if request.method in _MUTATING_METHODS and not _same_origin(request):
            await Response("Cross-origin mutation blocked", status_code=403)(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)


class SignedCookieSessionMiddleware:
    """Minimal signed cookie session for OAuth state storage."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        scope["session"] = _load_session_cookie(request.cookies.get(_SESSION_COOKIE))
        had_cookie = _SESSION_COOKIE in request.cookies

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                session = scope.get("session") or {}
                if session:
                    secure = (
                        os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
                    )
                    cookie = (
                        f"{_SESSION_COOKIE}={_dump_session_cookie(session)}; "
                        "Path=/; HttpOnly; SameSite=lax"
                    )
                    if secure:
                        cookie += "; Secure"
                    headers.append("set-cookie", cookie)
                elif had_cookie:
                    headers.append(
                        "set-cookie",
                        f"{_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=lax",
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)


# Public paths reachable without authentication. Everything else is default-deny
# (fail-closed). Prefixes cover the SPA bundle and the Phase-2 OIDC routes.
_PUBLIC_EXACT_PATHS = {
    "/health",
    "/favicon.ico",
    "/login",
    "/signup",
    "/logout",
    "/api/v1/auth/config",
    "/api/v1/auth/login",
    "/api/v1/auth/signup",
}
_PUBLIC_PATH_PREFIXES = ("/assets/", "/auth/")


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_EXACT_PATHS or any(
        path.startswith(p) for p in _PUBLIC_PATH_PREFIXES
    )


def _unauthenticated_response(request: Request) -> Response:
    """Branch the unauthenticated response by request kind.

    API/JSON → 401 JSON. Browser navigation → 303 redirect to /login?next=<original>.
    """
    accept = request.headers.get("accept", "")
    if request.url.path.startswith("/api/") or "application/json" in accept:
        return JSONResponse(
            {"detail": "Not authenticated", "code": "not_authenticated"},
            status_code=401,
        )
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)


class AuthGateMiddleware:
    """Fail-closed authentication gate.

    Reads the signed session cookie directly (order-independent of the session
    middleware), resolves the user, and either passes the request through with
    ``request.state.auth_user_id`` set, or returns an unauthenticated response.
    When AUTH_ENABLED is false, binds the bootstrap admin so downstream code
    always has a current user.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        from app.config import AUTH_ENABLED

        # Dev mode: no gating. The current user is bound lazily downstream
        # (get_current_user) on the request-scoped session, so the
        # gate never touches a database here (keeps it engine-agnostic for tests).
        if not AUTH_ENABLED:
            await self.app(scope, receive, send)
            return

        if request.method == "OPTIONS" or _is_public_path(request.url.path):
            await self.app(scope, receive, send)
            return

        from app.dependencies import SessionLocal
        from app.services import auth_service

        session = _load_session_cookie(request.cookies.get(_SESSION_COOKIE))
        db = SessionLocal()
        try:
            user = auth_service.resolve_session_user(db, session)
            uid = user.id if user else None
        finally:
            db.close()

        if uid is None:
            await _unauthenticated_response(request)(scope, receive, send)
            return

        scope.setdefault("state", {})["auth_user_id"] = uid
        await self.app(scope, receive, send)


# --- FastAPI App ---
app = FastAPI(
    title="The Sanctuary",
    description="Privacy-first legal case management.",
    version="1.0.0",
    lifespan=lifespan,
)

# Compression middleware (outermost - processes responses first)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(SignedCookieSessionMiddleware)

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
app.add_middleware(OriginGuardMiddleware)
app.add_middleware(AuthGateMiddleware)
# Must stay the LAST add_middleware(...) call so it remains outermost --
# see AccessLogMiddleware's docstring for why.
app.add_middleware(AccessLogMiddleware)

PROJECT_ROOT = Path(__file__).parent.parent
# Hashed SPA bundle. check_dir=False: the app must still boot (and say so on
# the SPA routes) when the frontend has not been built yet.
app.mount(
    "/assets",
    ImmutableStaticFiles(directory=str(FRONTEND_DIST / "assets"), check_dir=False),
    name="assets",
)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(PROJECT_ROOT / "static" / "favicon.png")


@app.get("/health")
async def health_check():
    """Lightweight endpoint for Docker health checks."""
    return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}


# Rate limiter setup
# slowapi's handler is typed for RateLimitExceeded specifically; Starlette's
# add_exception_handler wants a generic Exception handler. This is slowapi's
# own documented registration pattern — a stub mismatch, not a real bug.
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
    if is_api_path(request.url.path):
        response = error_response(
            429, "rate_limited", f"Too many attempts ({exc.detail}). Try again later."
        )
        return limiter._inject_headers(response, request.state.view_rate_limit)
    return _rate_limit_exceeded_handler(request, exc)


app.add_exception_handler(RateLimitExceeded, rate_limit_handler)  # type: ignore[arg-type]


# Error page defaults
def _page_error(status: int, title: str, message: str) -> HTMLResponse:
    """A dependency-free error page for browser navigations outside the SPA."""
    message = html.escape(message)
    body = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{status} · The Sanctuary</title>"
        "<style>body{font-family:system-ui,sans-serif;background:#0b1220;color:#dbe4f3;"
        "display:grid;place-items:center;height:100vh;margin:0}main{max-width:32rem;"
        "text-align:center}h1{font-size:1.4rem}a{color:#5eead4}</style></head><body>"
        f"<main><h1>{title}</h1><p>{message}</p><p><a href='/'>Back to the Sanctuary</a>"
        "</p></main></body></html>"
    )
    return HTMLResponse(body, status_code=status)


async def not_found_handler(request: Request, exc: Exception) -> Response:
    """JSON for the API, the SPA shell (which shows its not-found view) for pages."""
    if is_api_path(request.url.path):
        return http_error_response(exc, default_status=404)
    page = spa_index()
    page.status_code = 404 if page.status_code == 200 else page.status_code
    return page


async def server_error_handler(request: Request, exc: Exception) -> Response:
    logger = logging.getLogger(__name__)
    error_msg = str(exc.detail) if hasattr(exc, "detail") else str(exc)
    logger.error(f"Server error on {request.url.path}: {error_msg}", exc_info=True)
    if is_api_path(request.url.path):
        return http_error_response(exc, default_status=500)
    return _page_error(500, "Something went wrong", "An unexpected error occurred.")


async def secrets_error_handler(request: Request, exc: Exception) -> Response:
    """A missing/wrong SECRETS_ENCRYPTION_KEY is an operator problem the message
    explains (no secret in it) — surface it instead of an opaque 500."""
    logging.getLogger(__name__).error("Secrets error on %s: %s", request.url.path, exc)
    if is_api_path(request.url.path):
        return error_response(503, "secrets_key_unavailable", str(exc))
    return _page_error(503, "Encryption key unavailable", str(exc))


async def validation_error_handler(request: Request, exc: Exception) -> Response:
    if is_api_path(request.url.path):
        return http_error_response(exc, default_status=422)
    message = str(exc.detail) if hasattr(exc, "detail") else "Validation error"
    return _page_error(422, "Request could not be processed", message)


# Register exception handlers
app.add_exception_handler(404, not_found_handler)
app.add_exception_handler(500, server_error_handler)
app.add_exception_handler(422, validation_error_handler)
app.add_exception_handler(SecretsError, secrets_error_handler)


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    """Status-code handlers above win for 404/422/500; this covers every other
    status under /api/v1 (401, 403, 409, ...), whether raised as ApiError or as
    a plain HTTPException from a shared dependency such as get_current_admin."""
    if is_api_path(request.url.path):
        return http_error_response(exc, default_status=500)
    return await fastapi_http_exception_handler(request, exc)


app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]


async def request_validation_handler(
    request: Request, exc: RequestValidationError
) -> Response:
    """Malformed request bodies: uniform shape under /api/v1, FastAPI's elsewhere."""
    if is_api_path(request.url.path):
        return validation_error_response(exc)
    return await request_validation_exception_handler(request, exc)


app.add_exception_handler(RequestValidationError, request_validation_handler)  # type: ignore[arg-type]

from app.api import (
    home_router,
    ingestion_settings,
)
from app.api.auth import router as auth_router
from app.api.auth_oidc import router as auth_oidc_router
from app.api.settings_page import router as settings_page_router
from app.api.spa_pages import router as spa_pages_router
from app.api.v1 import router as api_v1_router

app.include_router(api_v1_router)
app.include_router(auth_router)
app.include_router(auth_oidc_router)
app.include_router(home_router)
app.include_router(spa_pages_router)
app.include_router(ingestion_settings.router)
app.include_router(settings_page_router)


if __name__ == "__main__":
    import uvicorn

    from app.config import DEBUG, HOST, PORT

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=DEBUG)

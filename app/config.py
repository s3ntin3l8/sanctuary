import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

# Python 3.12 deprecated sqlite3's default datetime adapter/converter. Without
# explicit registration, every DateTime column read or write raises a
# DeprecationWarning. Register ISO-8601 adapters per the recommended recipe
# in the sqlite3 docs — keeps SQLAlchemy's DateTime round-trip silent.
sqlite3.register_adapter(datetime, lambda d: d.isoformat(sep=" "))
sqlite3.register_adapter(date, lambda d: d.isoformat())

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL", f"sqlite:///{DATA_DIR / 'sanctuary.db'}"
)

# Application Settings
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
DEBUG = os.getenv("DEBUG", "False").lower() == "true"
INGEST_CONVERSION_TIMEOUT = int(os.getenv("INGEST_CONVERSION_TIMEOUT", "600"))

AI_BASE_URL = os.getenv("AI_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
AI_SUMMARY_MODEL = os.getenv("AI_SUMMARY_MODEL", "qwen3.5:9b")
AI_EMBED_MODEL = os.getenv("AI_EMBED_MODEL", "nomic-embed-text:v1.5")
AI_EMBED_DIM = int(os.getenv("AI_EMBED_DIM", "768"))  # nomic-embed-text default
AI_USER_CONTEXT = os.getenv("AI_USER_CONTEXT", "")
# Read timeout (seconds) for streaming AI calls. Local inference on long prompts
# can easily exceed 60s; the default of 600s gives slow local models headroom.
AI_READ_TIMEOUT = float(os.getenv("AI_READ_TIMEOUT", "600"))

# AI Provider Configuration (ollama, lmstudio, openai, or auto)
AI_PROVIDER = os.getenv("AI_PROVIDER", "ollama").lower()
AI_API_KEY = os.getenv("AI_API_KEY", "not-needed")

# --- Authentication / Accounts ---
# When false, the request is auto-bound to the bootstrap admin (single-user dev
# mode): there is always exactly one current_user and no login is required.
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() == "true"
# Self-service signup. Off by default — flip on (here or at runtime via the admin
# UI) once you intend to onboard additional users. New signups are regular users.
AUTH_SIGNUP_ENABLED = os.getenv("AUTH_SIGNUP_ENABLED", "false").lower() == "true"
# Optional code-driven provisioning of the first admin (e.g. Ansible). FRESH DB
# ONLY: both must be set, and they seed the primary admin exactly once when the
# database has no users. After setup they are inert — editing them does nothing;
# change the account from the UI. When unset, the one-time create-admin screen
# onboards the first admin instead.
BOOTSTRAP_ADMIN_EMAIL = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
# Signed-cookie session lifetime. Sessions older than this (by issued-at) are
# treated as logged out. Default 14 days.
SESSION_LIFETIME_SECONDS = int(os.getenv("SESSION_LIFETIME_SECONDS", "1209600"))

# OIDC / authentik (Phase 2). OIDC is active only when issuer + client id +
# secret are all set; otherwise the OIDC routes 404 and the login button hides.
OIDC_ISSUER = os.getenv("OIDC_ISSUER", "").strip().rstrip("/")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "").strip()
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "").strip()
OIDC_REDIRECT_URI = os.getenv(
    "OIDC_REDIRECT_URI", "http://localhost:8000/auth/oidc/callback"
)
OIDC_SCOPES = os.getenv("OIDC_SCOPES", "openid email profile")
OIDC_PROVIDER_NAME = os.getenv("OIDC_PROVIDER_NAME", "authentik")


def oidc_enabled() -> bool:
    return bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)


# Gmail OAuth Configuration
GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")
GMAIL_REDIRECT_URI = os.getenv(
    "GMAIL_REDIRECT_URI", "http://localhost:8000/api/ingest/gmail/oauth/callback"
)

# Scan folder ingest
SCAN_INGEST_ROOT = DATA_DIR / "scans"
SCAN_INCOMING_DIR = SCAN_INGEST_ROOT / "incoming"
SCAN_PROCESSING_DIR = SCAN_INGEST_ROOT / "processing"
SCAN_PROCESSED_DIR = SCAN_INGEST_ROOT / "processed"
SCAN_FAILED_DIR = SCAN_INGEST_ROOT / "failed"
SCAN_POLL_INTERVAL_SECONDS = int(os.getenv("SCAN_POLL_INTERVAL_SECONDS", "30"))

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

TIMEZONE = os.getenv("TIMEZONE", "Europe/Berlin")

CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://localhost:8000,http://host.docker.internal:3000,http://host.docker.internal:8000",
    ).split(",")
    if origin.strip()
]

# SQLite-specific connection pooling (StaticPool for single-connection)
_is_sqlite = SQLALCHEMY_DATABASE_URL.startswith("sqlite")

if _is_sqlite:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
else:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=3600,
    )


@event.listens_for(engine, "connect")
def load_sqlite_extensions(dbapi_conn, connection_record):
    """Load sqlite-vec extension and configure SQLite for better performance."""
    try:
        import sqlite_vec

        dbapi_conn.enable_load_extension(True)
        sqlite_vec.load(dbapi_conn)
        dbapi_conn.enable_load_extension(False)
    except (ImportError, Exception) as e:
        import logging

        logging.getLogger(__name__).warning(f"Failed to load sqlite-vec: {e}")

    if _is_sqlite:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=-64000")
        cursor.execute("PRAGMA temp_store=MEMORY")
        # Daemon-thread EAGER dispatch fans out 10+ concurrent writers on bundle
        # retry. Default busy_timeout=0 fails the loser immediately with
        # "database is locked" — silently, since dispatch_task swallows it.
        # 5s wasn't enough under the Wave 2B claim pipeline (extractor →
        # embedding → dedup judge can hold the lock for several seconds per
        # claim, while interactive page renders need to land their writes).
        # Bump to 30s; AI calls themselves don't hold the lock, so the
        # contention windows are short bursts of pure DB work.
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))

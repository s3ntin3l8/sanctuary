import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://sanctuary:sanctuary@localhost:5432/sanctuary",
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

# AI Provider Configuration. Registered instances always store "auto" (the UI
# no longer exposes a manual picker — API shape is probed at runtime); this env
# default only applies to the no-instance fallback and as an explicit override.
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

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_recycle=3600,
)


@event.listens_for(engine, "connect")
def register_pgvector_adapter(dbapi_conn, connection_record):
    """Register the pgvector type adapter on every new psycopg connection so
    `Vector` columns bind/read Python lists directly."""
    from pgvector.psycopg import register_vector

    register_vector(dbapi_conn)


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))

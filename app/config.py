import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, event, pool
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL", f"sqlite:///{DATA_DIR / 'sanctuary.db'}"
)

AI_BASE_URL = os.getenv("AI_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
AI_SUMMARY_MODEL = os.getenv("AI_SUMMARY_MODEL", "qwen3.5:9b")
AI_EMBED_MODEL = os.getenv("AI_EMBED_MODEL", "nomic-embed-text")
AI_SYSTEM_PROMPT = os.getenv("AI_SYSTEM_PROMPT", "")

# AI Provider Configuration (ollama, lmstudio, openai, or auto)
AI_PROVIDER = os.getenv("AI_PROVIDER", "ollama").lower()
AI_API_KEY = os.getenv("AI_API_KEY", "not-needed")

# Gmail OAuth Configuration
GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")
GMAIL_REDIRECT_URI = os.getenv("GMAIL_REDIRECT_URI", "http://localhost:8000/api/ingest/gmail/oauth/callback")

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
        poolclass=pool.StaticPool,
        pool_pre_ping=True,
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
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=-64000")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))

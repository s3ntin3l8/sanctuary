import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
# Load environment variables from .env file if it exists
load_dotenv(PROJECT_ROOT / ".env")

from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# Use DATABASE_URL environment variable if available, otherwise fallback to default
SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL", f"sqlite:///{DATA_DIR / 'sanctuary.db'}"
)

# Ollama configuration
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_SUMMARY_MODEL = os.getenv("OLLAMA_SUMMARY_MODEL", "qwen3.5:9b")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def load_sqlite_extensions(dbapi_conn, connection_record):
    """Load sqlite-vec extension for semantic search support."""
    try:
        import sqlite_vec

        dbapi_conn.enable_load_extension(True)
        sqlite_vec.load(dbapi_conn)
        dbapi_conn.enable_load_extension(False)
    except (ImportError, Exception) as e:
        # Fallback for environments where extension might be missing
        import logging

        logging.getLogger(__name__).warning(f"Failed to load sqlite-vec: {e}")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app.config import SessionLocal, engine, templates
from app.constants import REVIEW_FIELD_LABELS
from app.helpers import format_eur, format_relative_time
from app.models.database import (
    Case,
    CaseStatus,
    CostCategory,
    CostStatus,
    Deadline,
    Hearing,
    LegalCost,
)
from app.routers import actions, pages
from app.services.normalization import normalize_hm

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

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

    db: Session = SessionLocal()
    try:
        for seed in _SEED_CASES:
            if not db.get(Case, seed["id"]):
                db.add(Case(**seed))
        db.commit()

        if (
            db.query(Deadline)
            .filter(Deadline.case_id.in_([s["id"] for s in _SEED_CASES]))
            .count()
            == 0
        ):
            now = datetime.now(UTC).replace(second=0, microsecond=0)
            for seed in _SEED_DEADLINES:
                db.add(
                    Deadline(
                        case_id=seed["case_id"],
                        title=seed["title"],
                        description=seed["description"],
                        due_at=now + timedelta(days=seed["offset_days"]),
                    )
                )

        if (
            db.query(Hearing)
            .filter(Hearing.case_id.in_([s["id"] for s in _SEED_CASES]))
            .count()
            == 0
        ):
            base_time = datetime.now(UTC).replace(second=0, microsecond=0)
            for seed in _SEED_HEARINGS:
                scheduled_day = base_time + timedelta(days=seed["offset_days"])
                db.add(
                    Hearing(
                        case_id=seed["case_id"],
                        title=seed["title"],
                        description=seed["description"],
                        location=seed["location"],
                        scheduled_for=scheduled_day.replace(
                            hour=seed["hour"],
                            minute=seed["minute"],
                        ),
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


# --- FastAPI App ---
app = FastAPI(
    title="The Sanctuary",
    description="Privacy-first legal case management.",
    version="1.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter

# Mount static files early
PROJECT_ROOT = Path(__file__).parent.parent
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")

# -- Seed Data ---
_SEED_CASES = [
    {
        "id": "_TRIAGE",
        "title": "Triage Inbox",
        "court_id": "",
        "status": CaseStatus.INTAKE,
    },
    {
        "id": "ADV-992-K",
        "title": "Vane vs. Vane: Divorce & Assets",
        "court_id": "2024-FL-DR-00992",
        "status": CaseStatus.DISCOVERY,
    },
    {
        "id": "ADV-804-M",
        "title": "Smith Construction vs. City Council",
        "court_id": "2024-CV-00804",
        "status": CaseStatus.PRE_TRIAL,
    },
    {
        "id": "REF-441-22",
        "title": "Mercury Tech IP Dispute",
        "court_id": "2022-IP-HC-00441",
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


templates.env.globals["review_field_labels"] = REVIEW_FIELD_LABELS
templates.env.filters["hm"] = normalize_hm
templates.env.globals["format_eur"] = format_eur
templates.env.filters["format_relative_time"] = format_relative_time
templates.env.filters["urlencode"] = quote

# Safe markdown filter to strip tags
from markupsafe import Markup


def safe_markdown(value: str) -> Markup:
    """Return a safe string with HTML tags stripped."""
    return Markup(str(value).replace("<", "&lt;").replace(">", "&gt;"))


templates.env.filters["safe_markdown"] = safe_markdown

# Rate limiter setup
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(pages.router)
# Rate limiter disabled for actions router - causes 422 with multipart forms
app.include_router(actions.router)

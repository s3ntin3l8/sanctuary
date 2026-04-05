import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import quote
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.config import engine, SessionLocal, templates
from app.constants import REVIEW_FIELD_LABELS
from app.models.database import (
    Base,
    Case,
    CaseStatus,
    CostCategory,
    CostStatus,
    Deadline,
    Hearing,
    LegalCost,
)
from app.routers import pages, actions
from app.helpers import format_eur, format_relative_time
from app.services.normalization import normalize_hm

_SEED_CASES = [
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
        "location": "County Courthouse, Courtroom 12",
        "offset_days": 9,
        "hour": 14,
        "minute": 0,
    },
]

_SEED_COSTS = [
    {
        "case_id": "ADV-992-K",
        "category": "vorschuss",
        "status": "bezahlt",
        "title": "Gerichtskostenvorschuss 1. Instanz",
        "rvg_position": "KV GKG Nr. 1210",
        "amount_net": 2710.00,
        "vat_rate": 0.0,
        "amount_gross": 2710.00,
        "amount_paid": 2710.00,
        "streitwert": 150000.0,
        "is_reimbursable": True,
        "offset_issued": -45,
        "offset_due": -42,
        "offset_paid": -40,
    },
    {
        "case_id": "ADV-992-K",
        "category": "anwaltskosten",
        "status": "bezahlt",
        "title": "Verfahrensgebühr 1. Instanz",
        "rvg_position": "Nr. 3100 VV RVG",
        "amount_net": 2562.30,
        "vat_rate": 0.19,
        "amount_gross": 3049.14,
        "amount_paid": 3049.14,
        "streitwert": 150000.0,
        "gebuehren_faktor": 1.3,
        "is_reimbursable": True,
        "offset_issued": -44,
        "offset_due": -30,
        "offset_paid": -28,
    },
    {
        "case_id": "ADV-992-K",
        "category": "anwaltskosten",
        "status": "offen",
        "title": "Terminsgebühr",
        "rvg_position": "Nr. 3104 VV RVG",
        "amount_net": 2365.20,
        "vat_rate": 0.19,
        "amount_gross": 2814.59,
        "amount_paid": 0.0,
        "streitwert": 150000.0,
        "gebuehren_faktor": 1.2,
        "is_reimbursable": True,
        "offset_issued": -10,
        "offset_due": 14,
        "offset_paid": None,
    },
    {
        "case_id": "ADV-992-K",
        "category": "auslagen",
        "status": "offen",
        "title": "Auslagenpauschale",
        "rvg_position": "Nr. 7001 VV RVG",
        "amount_net": 20.00,
        "vat_rate": 0.19,
        "amount_gross": 23.80,
        "amount_paid": 0.0,
        "streitwert": None,
        "is_reimbursable": True,
        "offset_issued": -10,
        "offset_due": 14,
        "offset_paid": None,
    },
    {
        "case_id": "ADV-804-M",
        "category": "vorschuss",
        "status": "bezahlt",
        "title": "Gerichtskostenvorschuss 1. Instanz",
        "rvg_position": "KV GKG Nr. 1210",
        "amount_net": 1974.00,
        "vat_rate": 0.0,
        "amount_gross": 1974.00,
        "amount_paid": 1974.00,
        "streitwert": 85000.0,
        "is_reimbursable": True,
        "offset_issued": -60,
        "offset_due": -57,
        "offset_paid": -55,
    },
    {
        "case_id": "ADV-804-M",
        "category": "anwaltskosten",
        "status": "bezahlt",
        "title": "Verfahrensgebühr 1. Instanz",
        "rvg_position": "Nr. 3100 VV RVG",
        "amount_net": 1899.30,
        "vat_rate": 0.19,
        "amount_gross": 2260.17,
        "amount_paid": 2260.17,
        "streitwert": 85000.0,
        "gebuehren_faktor": 1.3,
        "is_reimbursable": True,
        "offset_issued": -58,
        "offset_due": -45,
        "offset_paid": -43,
    },
    {
        "case_id": "ADV-804-M",
        "category": "sachverstaendiger",
        "status": "offen",
        "title": "Sachverständigengebühr (Baugutachten)",
        "rvg_position": "JVEG § 9",
        "amount_net": 1200.00,
        "vat_rate": 0.19,
        "amount_gross": 1428.00,
        "amount_paid": 0.0,
        "streitwert": None,
        "is_reimbursable": True,
        "offset_issued": -5,
        "offset_due": 21,
        "offset_paid": None,
    },
    {
        "case_id": "ADV-804-M",
        "category": "anwaltskosten_gegner",
        "status": "strittig",
        "title": "Anwaltskosten Gegner (Kostenrisiko §91 ZPO)",
        "rvg_position": "Nr. 3100 VV RVG (gegnerisch)",
        "amount_net": 1899.30,
        "vat_rate": 0.19,
        "amount_gross": 2260.17,
        "amount_paid": 0.0,
        "streitwert": 85000.0,
        "gebuehren_faktor": 1.3,
        "is_reimbursable": False,
        "notes": "Kostenfestsetzungsantrag des Gegners erwartet nach Urteil",
        "offset_issued": None,
        "offset_due": None,
        "offset_paid": None,
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    import sqlite3
    from alembic.config import Config as _AlembicConfig
    from alembic import command

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
            now = datetime.utcnow().replace(second=0, microsecond=0)
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
            base_time = datetime.utcnow().replace(second=0, microsecond=0)
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
            now = datetime.utcnow().replace(second=0, microsecond=0)

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


app = FastAPI(title="The Sanctuary", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

templates.env.globals["review_field_labels"] = REVIEW_FIELD_LABELS
templates.env.filters["hm"] = normalize_hm
templates.env.globals["format_eur"] = format_eur
templates.env.filters["format_relative_time"] = format_relative_time
templates.env.filters["urlencode"] = quote

app.include_router(pages.router)
app.include_router(actions.router)

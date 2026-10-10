"""Settings → AI & models (global, admin-only): endpoints, roles, index, workers."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.v1.errors import ApiError
from app.core.rate_limit import limiter
from app.core.timezone import now_utc
from app.dependencies import get_current_admin, get_db
from app.models.database import Document
from app.models.enums import AuditEventType
from app.schemas.settings import (
    AiHealth,
    AiInstance,
    AiInstanceInput,
    AiRole,
    AiSettingsView,
    ConcurrencyResult,
    ConcurrencyUpdate,
    DebugRedactUpdate,
    EmbedIndex,
    ExtractionEngineUpdate,
    ModelsView,
    ReindexJob,
    Role,
    RoleHealthView,
    RoleUpdate,
    RoleUpdateResult,
)
from app.services import audit_service
from app.services.ai_config import (
    _get_ai_section,
    _make_id,
    delete_instance,
    get_embed_config,
    get_instance,
    is_external_endpoint,
    list_instances,
    save_instance,
    set_active,
)
from app.services.ai_provider import chat_provider, embed_provider, ocr_provider
from app.services.ai_settings_service import (
    ROLE_FIELD,
    ROLE_HINT,
    ROLE_LABEL,
    ROLES,
    fetch_models,
    probe_embed_dim,
    probe_instance,
    probe_role_health,
    provider_for,
    resolve_roles,
)
from app.services.embeddings import verify_embedding_dim
from app.services.user_settings_service import (
    get_extraction_engine,
    get_ocr_concurrency,
    get_reindex_job,
    get_worker_concurrency,
    set_ai_debug_redact,
    set_extraction_engine,
    set_ocr_concurrency,
    set_reindex_running,
    set_worker_concurrency,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/settings/ai",
    tags=["settings"],
    dependencies=[Depends(get_current_admin)],
)

NO_KEY = "not-needed"


def _instance(inst: dict[str, Any]) -> AiInstance:
    key = inst.get("api_key") or NO_KEY
    return AiInstance(
        id=inst["id"],
        label=inst.get("label", ""),
        base_url=inst.get("base_url", ""),
        has_api_key=key != NO_KEY,
        is_external=is_external_endpoint(inst.get("base_url", "")),
        summary_model=inst.get("summary_model", "") or "",
        embed_model=inst.get("embed_model", "") or "",
        embed_dim=inst.get("embed_dim"),
        ocr_model=inst.get("ocr_model", "") or "",
    )


def _role(role: str, inst: dict[str, Any]) -> AiRole:
    return AiRole(
        role=role,  # type: ignore[arg-type]
        label=ROLE_LABEL[role],
        hint=ROLE_HINT[role],
        active_id=inst.get("id") or None,
        model=inst.get(ROLE_FIELD[role], "") or "",
        embed_dim=inst.get("embed_dim") if role == "embed" else None,
    )


def _embed_index(db: Session) -> EmbedIndex:
    cfg = get_embed_config(db)
    ok, actual = verify_embedding_dim(db, cfg.embed_dim)
    return EmbedIndex(
        dim=cfg.embed_dim,
        model=cfg.embed_model or "",
        index_dim=actual,
        mismatch=not ok and actual is not None,
    )


# How long a finished (done/failed) reindex stays on the settings page. The
# stored record is only the last run's result; showing "Reindex failed" for a
# run that ended days ago just hides whether anything is wrong now.
_REINDEX_RESULT_VISIBLE = timedelta(hours=24)


def _reindex_job(db: Session) -> ReindexJob | None:
    job = get_reindex_job(db)
    if not job or not job.get("status"):
        return None
    if job["status"] != "running" and job.get("ended_at"):
        try:
            ended = datetime.fromisoformat(job["ended_at"])
        except ValueError:
            return None
        if now_utc() - ended > _REINDEX_RESULT_VISIBLE:
            return None
    return ReindexJob(**job)


def _reload_providers(db: Session) -> None:
    for provider in (chat_provider, embed_provider, ocr_provider):
        provider.reload_from_db(db)


@router.get("", response_model=AiSettingsView)
def ai_settings(db: Session = Depends(get_db)):
    """Configuration only — no network probes (see /health and /models)."""
    resolved = resolve_roles(db)
    return AiSettingsView(
        instances=[_instance(i) for i in list_instances(db)],
        roles=[_role(r, resolved[r]) for r in ROLES],
        extraction_engine=get_extraction_engine(db),  # type: ignore[arg-type]
        tesseract_available=shutil.which("tesseract") is not None,
        worker_concurrency=get_worker_concurrency(db),
        ocr_concurrency=get_ocr_concurrency(db),
        embed_index=_embed_index(db),
        reindex_job=_reindex_job(db),
    )


@router.get("/health", response_model=RoleHealthView)
async def role_health(db: Session = Depends(get_db)):
    health = await probe_role_health(db)
    return RoleHealthView(**{r: AiHealth(**health[r]) for r in ROLES})


# --- Endpoints (instances) ---------------------------------------------------


@router.post("/instances", response_model=AiInstance, status_code=201)
def create_instance(body: AiInstanceInput, db: Session = Depends(get_db)):
    instance = {
        "id": _make_id(),
        "label": body.label.strip(),
        "base_url": body.base_url.strip().rstrip("/"),
        # API shape is auto-detected at call time; never set manually.
        "provider": "auto",
        "api_key": (body.api_key or "").strip() or NO_KEY,
        "summary_model": "",
        "embed_model": "",
        "embed_dim": None,
        "ocr_model": "",
    }
    save_instance(db, instance)
    return _instance(instance)


@router.put("/instances/{instance_id}", response_model=AiInstance)
def update_instance(
    instance_id: str, body: AiInstanceInput, db: Session = Depends(get_db)
):
    existing = get_instance(db, instance_id)
    if not existing:
        raise ApiError(404, "not_found", "Endpoint not found.")
    api_key = existing.get("api_key", NO_KEY)
    if body.api_key is not None:
        api_key = body.api_key.strip() or NO_KEY
    instance = {
        **existing,
        "label": body.label.strip(),
        "base_url": body.base_url.strip().rstrip("/"),
        "provider": "auto",
        "api_key": api_key,
    }
    save_instance(db, instance)
    _reload_providers(db)
    return _instance(instance)


@router.delete("/instances/{instance_id}", status_code=204)
def remove_instance(instance_id: str, db: Session = Depends(get_db)):
    if get_instance(db, instance_id) is None:
        raise ApiError(404, "not_found", "Endpoint not found.")
    ai = _get_ai_section(db)
    if instance_id in (
        ai.get("active_chat_id"),
        ai.get("active_embed_id"),
        ai.get("active_ocr_id"),
    ):
        raise ApiError(
            409,
            "instance_active",
            "Cannot delete the active endpoint — switch a role to another first.",
        )
    delete_instance(db, instance_id)


@router.post("/instances/{instance_id}/test", response_model=AiHealth)
@limiter.limit("20/minute")
async def test_instance(
    instance_id: str, request: Request, db: Session = Depends(get_db)
):
    inst = get_instance(db, instance_id)
    if not inst:
        raise ApiError(404, "not_found", "Endpoint not found.")
    return AiHealth(**await probe_instance(inst))


@router.get("/instances/{instance_id}/models", response_model=ModelsView)
async def instance_models(instance_id: str, db: Session = Depends(get_db)):
    inst = get_instance(db, instance_id)
    if not inst:
        raise ApiError(404, "not_found", "Endpoint not found.")
    return ModelsView(**await fetch_models(inst))


# --- Roles -------------------------------------------------------------------


@router.put("/roles/{role}", response_model=RoleUpdateResult)
async def set_role(role: Role, body: RoleUpdate, db: Session = Depends(get_db)):
    """Point a role at an endpoint and, if ``model`` is set, pick its model."""
    inst = get_instance(db, body.instance_id)
    if not inst:
        raise ApiError(404, "not_found", "Endpoint not found.")

    field = ROLE_FIELD[role]
    model = body.model.strip()
    set_active(db, role, body.instance_id)

    warning: str | None = None
    if model:
        inst = {**inst, field: model}
        if role == "embed":
            dim, err = await probe_embed_dim(inst)
            if dim:
                inst["embed_dim"] = dim
            elif not inst.get("embed_dim"):
                # Keep the user's pick; embeddings stay gated on a known dim.
                warning = (
                    "Model saved, but the embedding dimension could not be "
                    f"detected: {err}. Embeddings stay disabled until the endpoint "
                    "is reachable — re-select the model to retry."
                )
        save_instance(db, inst)

    provider = provider_for(role)
    provider.reload_from_db(db)
    try:
        health = await provider.probe_health()
        if health.get("ok"):
            health["provider"] = str(await provider.get_type())
    except Exception as exc:  # unreachable endpoint: report, don't fail the save
        health = {"ok": False, "detail": str(exc)}

    return RoleUpdateResult(
        role=_role(role, inst),
        health=AiHealth(**health),
        warning=warning,
        embed_index=_embed_index(db),
    )


# --- Embedding index ---------------------------------------------------------


def _start_reindex(db: Session, *, embed_dim: int) -> ReindexJob:
    from app.tasks.dispatch import dispatch_task
    from app.tasks.generate_embedding import reindex_all_embeddings_task

    total = db.query(Document).filter(Document.content.isnot(None)).count()
    set_reindex_running(db, total=total, embed_dim=embed_dim)
    db.commit()
    dispatch_task(reindex_all_embeddings_task)
    job = _reindex_job(db)
    assert job is not None
    return job


def _refuse_if_running(db: Session) -> None:
    existing = get_reindex_job(db)
    if existing and existing.get("status") == "running":
        raise ApiError(409, "reindex_running", "A reindex is already in flight.")


@router.post("/reindex", response_model=ReindexJob)
@limiter.limit("5/minute")
def reindex(request: Request, db: Session = Depends(get_db)):
    """Re-embed every document with the current settings (no column change)."""
    _refuse_if_running(db)
    embed_provider.reload_from_db(db)
    cfg = get_embed_config(db)
    audit_service.record(db, AuditEventType.MAINTENANCE_REINDEX_DOCUMENTS)
    return _start_reindex(db, embed_dim=cfg.embed_dim)


@router.post("/rebuild-index", response_model=ReindexJob)
@limiter.limit("5/minute")
def rebuild_index(request: Request, db: Session = Depends(get_db)):
    """Resize both pgvector columns to the configured dimension, then re-embed."""
    cfg = get_embed_config(db)
    embed_dim = cfg.embed_dim
    if not (64 <= embed_dim <= 4096):
        raise ApiError(
            400, "embed_dim_out_of_range", f"embed_dim={embed_dim} out of range 64–4096"
        )
    _refuse_if_running(db)
    embed_provider.reload_from_db(db)
    try:
        # A pgvector column can only change dimension once every row is
        # NULL/empty: chunks are purely derived (delete them); claims are
        # domain data (clear only the embedding column).
        db.execute(text("DELETE FROM document_chunks"))
        db.execute(
            text(
                f"ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector({embed_dim})"
            )
        )
        db.execute(text("UPDATE claims SET embedding = NULL"))
        db.execute(
            text(f"ALTER TABLE claims ALTER COLUMN embedding TYPE vector({embed_dim})")
        )
        audit_service.record(db, AuditEventType.MAINTENANCE_REBUILD_INDEX)
        db.commit()
    except Exception as exc:
        logger.error("Failed to resize embedding columns to dim=%s: %s", embed_dim, exc)
        db.rollback()
        raise ApiError(
            500, "index_resize_failed", "Index resize failed — see server log"
        ) from exc
    return _start_reindex(db, embed_dim=embed_dim)


@router.get("/reindex/status", response_model=ReindexJob | None)
def reindex_status(db: Session = Depends(get_db)):
    return _reindex_job(db)


# --- Workers and engines -----------------------------------------------------


@router.put("/extraction-engine", response_model=ExtractionEngineUpdate)
def set_engine(body: ExtractionEngineUpdate, db: Session = Depends(get_db)):
    set_extraction_engine(db, body.engine)
    return body


@router.put("/worker-concurrency", response_model=ConcurrencyResult)
def worker_concurrency(body: ConcurrencyUpdate, db: Session = Depends(get_db)):
    from app.services.worker_control import apply_ai_concurrency

    set_worker_concurrency(db, body.concurrency)
    res = apply_ai_concurrency(body.concurrency)
    return ConcurrencyResult(
        concurrency=body.concurrency, applied_live=bool(res["live"])
    )


@router.put("/ocr-concurrency", response_model=ConcurrencyResult)
def ocr_concurrency(body: ConcurrencyUpdate, db: Session = Depends(get_db)):
    from app.services.ocr_slots import set_limit as set_ocr_slot_limit
    from app.services.worker_control import apply_ocr_concurrency

    set_ocr_concurrency(db, body.concurrency)
    set_ocr_slot_limit(body.concurrency)
    res = apply_ocr_concurrency(body.concurrency)
    return ConcurrencyResult(
        concurrency=body.concurrency, applied_live=bool(res["live"])
    )


@router.put("/debug-redact", response_model=DebugRedactUpdate)
def debug_redact(body: DebugRedactUpdate, db: Session = Depends(get_db)):
    set_ai_debug_redact(db, body.enabled)
    return body

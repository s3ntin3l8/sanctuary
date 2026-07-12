"""AI configuration settings endpoints: instance CRUD, model discovery, index rebuild."""

import json
import logging
from html import escape
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import DATA_DIR, templates
from app.core.rate_limit import limiter
from app.dependencies import get_db
from app.models.enums import AuditEventType
from app.services import audit_service
from app.services.ai_config import (
    delete_instance,
    get_embed_config,
    get_instance,
    save_instance,
    set_active,
    set_user_context,
)
from app.services.ai_provider import chat_provider, embed_provider, ocr_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/ai", tags=["settings"])


def _status_pill(health: dict, provider_label: str | None = None) -> str:
    color = "var(--color-primary)" if health["ok"] else "var(--color-error)"
    symbol = "check_circle" if health["ok"] else "error"
    label = health["detail"]
    if provider_label and health["ok"]:
        label = f"{provider_label} · {label}"
    return (
        f'<span class="inline-flex items-center gap-1.5 text-xs">'
        f'<span class="material-symbols-outlined text-[14px]" style="color:{color}">{symbol}</span>'
        f'<span style="color:{color}">{escape(label)}</span>'
        f"</span>"
    )


def _toast(ok: bool, message: str) -> str:
    color = "var(--color-primary)" if ok else "var(--color-error)"
    symbol = "check_circle" if ok else "error"
    return (
        f'<span class="inline-flex items-center gap-1.5 text-xs">'
        f'<span class="material-symbols-outlined text-[14px]" style="color:{color}">{symbol}</span>'
        f'<span style="color:{color}">{escape(message)}</span>'
        f"</span>"
    )


async def _probe_instance(inst: dict) -> dict:
    """Probe health for a specific instance config dict."""
    return await chat_provider.probe_health(config=inst)


async def _probe_embed_dim(inst: dict) -> tuple[int | None, str | None]:
    """Probe the embedding endpoint with a tiny input and return (dim, error_msg)."""
    from app.services.ai_provider import get_embedding_params_for

    model = inst.get("embed_model", "").strip()
    if not model:
        return None, "no embed_model set"

    try:
        params = await get_embedding_params_for(inst, model, "probe")
    except (RuntimeError, Exception) as e:
        return None, str(e)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                params["url"], json=params["json"], headers=params["headers"]
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return None, f"embed probe failed: {e}"

    vec = data.get("embedding") or (
        data.get("data", [{}])[0].get("embedding")
        if isinstance(data.get("data"), list) and data["data"]
        else None
    )
    if not vec:
        return None, "unexpected response shape from embed endpoint"

    return len(vec), None


def _categorize_models(models: list[str]) -> dict[str, list[str]]:
    """Categorize models into chat, embed, and ocr based on name heuristics.

    A model can appear in multiple categories — Qwen3.5-VL for instance is
    both chat-capable and OCR-capable. The select widgets filter by category
    so multi-category models naturally show up in each relevant dropdown.
    """
    chat = []
    embed = []
    ocr = []

    embed_keywords = ["embed", "similarity", "bert", "nomic", "minilm", "mxbai"]
    ocr_keywords = ["ocr", "chandra", "vl", "vision", "docling"]

    for m in models:
        name_lower = m.lower()
        is_embed = any(kw in name_lower for kw in embed_keywords)
        is_ocr = any(kw in name_lower for kw in ocr_keywords)
        if is_embed:
            embed.append(m)
        if is_ocr:
            ocr.append(m)
        if not is_embed:
            # Anything not strictly an embedding model is a candidate chat
            # model. Vision-LLMs (e.g. qwen-vl) can drive chat too.
            chat.append(m)

    return {"chat": chat, "embed": embed, "ocr": ocr}


async def _fetch_models(inst: dict) -> dict[str, list[str]]:
    """Fetch and categorize available models from a specific instance."""
    base_url = inst.get("base_url", "").strip().rstrip("/")
    provider = inst.get("provider", "auto")
    api_key = inst.get("api_key", "not-needed")

    from app.services.ai_provider import ProviderType, detect_provider

    try:
        ptype = (
            await detect_provider(base_url)
            if provider == "auto"
            else ProviderType(provider)
        )
    except (RuntimeError, Exception):
        return {"chat": [], "embed": []}

    all_models: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            if ptype == ProviderType.OLLAMA:
                resp = await client.get(f"{base_url}/api/tags")
                if resp.status_code == 200:
                    all_models = [m["name"] for m in resp.json().get("models", [])]
            else:
                headers = (
                    {"Authorization": f"Bearer {api_key}"}
                    if api_key != "not-needed"
                    else {}
                )
                resp = await client.get(f"{base_url}/v1/models", headers=headers)
                if resp.status_code == 200:
                    all_models = [m["id"] for m in resp.json().get("data", [])]
    except Exception as e:
        logger.warning(f"Model discovery failed for {base_url}: {e}")

    return _categorize_models(all_models)


def _model_options(models: list[str], selected: str) -> str:
    if not models:
        return f'<option value="{escape(selected, quote=True)}" selected>{escape(selected)}</option>'
    opts = []
    # No saved model (or it's not among the discovered ones): show an explicit
    # placeholder rather than letting the browser silently display the first
    # option as if chosen. Selecting a real model then always changes the value,
    # firing the select's `change` trigger so set_role actually persists it.
    if not selected or selected not in models:
        opts.append('<option value="" selected disabled>— select a model —</option>')
    opts += [
        f'<option value="{escape(m, quote=True)}" {"selected" if m == selected else ""}>'
        f"{escape(m)}</option>"
        for m in models
    ]
    return "".join(opts)


# ---------------------------------------------------------------------------
# Role cards — shared construction for the full-page GET and any mutation
# (e.g. adding an endpoint) that needs to OOB-refresh all three role cards.
# ---------------------------------------------------------------------------

_ROLE_META = [
    (
        "chat",
        "Chat",
        "summary_model",
        "forum",
        "Powers case briefs, chat answers, and document summaries.",
    ),
    (
        "embed",
        "Embeddings",
        "embed_model",
        "scatter_plot",
        "Powers semantic search and the vector index.",
    ),
    (
        "ocr",
        "OCR",
        "ocr_model",
        "document_scanner",
        "Reads scanned or stamped PDFs when the Chandra engine is active.",
    ),
]


async def build_role_cards(db) -> list[dict]:
    """Build the three role cards (chat/embed/ocr): each resolves to its
    active instance, stored model, live health, and discovered model options.

    Shared by the full-page GET (`settings_page.settings_ai`) and
    `create_instance` — a single source of truth so a newly added endpoint
    (or any active-instance change) can be reflected via an OOB re-render
    without duplicating this construction logic.
    """
    import asyncio

    from app.services.ai_config import _resolve_active

    resolved = {role: _resolve_active(db, role) for role, *_ in _ROLE_META}
    inst_by_id = {inst["id"]: inst for inst in resolved.values() if inst.get("id")}
    ids = list(inst_by_id)

    # Discover models and probe health for each active instance up front so
    # cards are populated without a manual Test/Discover. Deduped by id so a
    # shared endpoint (e.g. chat+ocr on the same box) is only queried once.
    if ids:
        fetched, health_results = await asyncio.gather(
            asyncio.gather(*[_fetch_models(inst_by_id[i]) for i in ids]),
            asyncio.gather(
                *[chat_provider.probe_health(config=inst_by_id[i]) for i in ids]
            ),
        )
    else:
        fetched, health_results = [], []
    models_by_id = dict(zip(ids, fetched, strict=True))
    health_by_id = dict(zip(ids, health_results, strict=True))

    role_cards = []
    for role, label, field, icon, hint in _ROLE_META:
        inst = resolved[role]
        aid = inst.get("id", "")
        cats = models_by_id.get(aid, {})
        role_cards.append(
            {
                "role": role,
                "label": label,
                "icon": icon,
                "hint": hint,
                "active_id": aid,
                "model": inst.get(field, ""),
                "options": _model_options(cats.get(role, []), inst.get(field, "")),
                "health": (
                    health_by_id.get(aid, {"ok": False, "detail": "Not tested"})
                    if aid
                    else {"ok": False, "detail": "No endpoint configured"}
                ),
                "embed_dim": inst.get("embed_dim") if role == "embed" else None,
            }
        )
    return role_cards


# ---------------------------------------------------------------------------
# Instance CRUD
# ---------------------------------------------------------------------------


@router.post("/instances", response_class=HTMLResponse)
async def create_instance(
    request: Request,
    label: str = Form("New Instance"),
    base_url: str = Form("http://127.0.0.1:11434"),
    api_key: str = Form("not-needed"),
    summary_model: str = Form(""),
    embed_model: str = Form(""),
    ocr_model: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.ai_config import _make_id

    inst_id = _make_id()
    instance: dict[str, Any] = {
        "id": inst_id,
        "label": label.strip() or "New Instance",
        "base_url": base_url.strip().rstrip("/"),
        # API shape is auto-detected at call time; never set manually.
        "provider": "auto",
        "api_key": api_key.strip() or "not-needed",
        "summary_model": summary_model.strip(),
        "embed_model": embed_model.strip(),
        "embed_dim": None,
        "ocr_model": ocr_model.strip(),
    }

    if embed_model.strip():
        dim, err = await _probe_embed_dim(instance)
        if err:
            return HTMLResponse(_toast(False, f"Could not detect embed dim: {err}"))
        instance["embed_dim"] = dim

    save_instance(db, instance)
    health = await _probe_instance(instance)
    from app.services.ai_config import _get_ai_section, list_instances

    ai = _get_ai_section(db)
    # This is the first endpoint the resolver has to choose from — role cards
    # (which fall back to instances[0] when no active id is set) now resolve
    # to it too, so OOB-refresh them in the same response rather than leaving
    # the role selectors stale until a manual refresh.
    role_cards = await build_role_cards(db)
    return templates.TemplateResponse(
        request,
        "partials/settings/_ai_instance_created.html",
        {
            "inst": instance,
            "health": health,
            "expanded": True,
            "active_chat_id": ai.get("active_chat_id", ""),
            "active_embed_id": ai.get("active_embed_id", ""),
            "active_ocr_id": ai.get("active_ocr_id", ""),
            "role_cards": role_cards,
            "instances": list_instances(db),
        },
        headers={"HX-Reswap": "beforeend", "HX-Retarget": "#ai-instances"},
    )


@router.post("/instances/{instance_id}", response_class=HTMLResponse)
async def save_instance_route(
    instance_id: str,
    request: Request,
    label: str = Form(""),
    base_url: str = Form(""),
    api_key: str = Form(""),
    summary_model: str = Form(""),
    embed_model: str = Form(""),
    ocr_model: str = Form(""),
    db: Session = Depends(get_db),
):
    existing = get_instance(db, instance_id)
    if not existing:
        return HTMLResponse(_toast(False, "Instance not found"), status_code=404)

    # The Connections form is endpoint-only (label/url/provider/key); model
    # fields are owned by the role cards. A blank model field here means "keep
    # what's stored", so endpoint saves never wipe or re-probe models.
    submitted_embed = embed_model.strip()
    instance = {
        "id": instance_id,
        "label": label.strip() or existing.get("label", "Instance"),
        "base_url": (base_url.strip() or existing.get("base_url", "")).rstrip("/"),
        # API shape is auto-detected; normalize any legacy manual value to auto.
        "provider": "auto",
        "api_key": api_key.strip() or existing.get("api_key", "not-needed"),
        "summary_model": summary_model.strip() or existing.get("summary_model", ""),
        "embed_model": submitted_embed or existing.get("embed_model", ""),
        "embed_dim": existing.get("embed_dim"),
        "ocr_model": ocr_model.strip() or existing.get("ocr_model", ""),
    }

    # Only re-probe the dimension when an embed model was explicitly submitted.
    if submitted_embed:
        dim, err = await _probe_embed_dim(instance)
        if dim:
            instance["embed_dim"] = dim
        elif not existing.get("embed_dim"):
            return HTMLResponse(_toast(False, f"Could not detect embed dim: {err}"))

    save_instance(db, instance)
    chat_provider.reload_from_db(db)
    embed_provider.reload_from_db(db)
    ocr_provider.reload_from_db(db)

    # Warn if this is the active embed instance and dim no longer matches vec0
    from app.services.ai_config import _get_ai_section
    from app.services.embeddings import verify_vec0_dim

    ai = _get_ai_section(db)
    dim_warning = ""
    embed_dim = instance.get("embed_dim")
    if ai.get("active_embed_id") == instance_id and embed_dim:
        dim_ok, actual_dim = verify_vec0_dim(db, int(embed_dim))
        if not dim_ok and actual_dim is not None:
            dim_warning = (
                f"Vector index dim mismatch: index={actual_dim}, "
                f"config={instance['embed_dim']}. "
                f"<button type='button' hx-post='/api/settings/ai/rebuild-index' "
                f"hx-target='#rebuild-result' hx-swap='innerHTML' "
                f"class='underline font-bold ml-1'>Rebuild index</button>"
            )

    return templates.TemplateResponse(
        request,
        "partials/settings/_ai_instance_row.html",
        {
            "inst": instance,
            "health": {"ok": None, "detail": "✓ Saved"},
            "expanded": True,
            "dim_warning": dim_warning,
            "active_chat_id": ai.get("active_chat_id", ""),
            "active_embed_id": ai.get("active_embed_id", ""),
            "active_ocr_id": ai.get("active_ocr_id", ""),
        },
    )


@router.delete("/instances/{instance_id}", response_class=HTMLResponse)
async def delete_instance_route(
    instance_id: str,
    db: Session = Depends(get_db),
):
    from app.services.ai_config import _get_ai_section

    ai = _get_ai_section(db)
    if (
        ai.get("active_chat_id") == instance_id
        or ai.get("active_embed_id") == instance_id
        or ai.get("active_ocr_id") == instance_id
    ):
        return HTMLResponse(
            _toast(
                False,
                "Cannot delete the active instance — switch to another first",
            ),
            headers={
                "HX-Retarget": f"#delete-error-{instance_id}",
                "HX-Reswap": "innerHTML",
            },
        )
    delete_instance(db, instance_id)
    return HTMLResponse("")


# ---------------------------------------------------------------------------
# Per-instance test and model discovery
# ---------------------------------------------------------------------------


@router.post("/instances/{instance_id}/test", response_class=HTMLResponse)
@limiter.limit("20/minute")
async def test_instance(
    instance_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    inst = get_instance(db, instance_id)
    if not inst:
        return HTMLResponse(_toast(False, "Instance not found"), status_code=404)
    health = await _probe_instance(inst)
    from app.services.ai_provider import ProviderType, detect_provider

    provider = inst.get("provider", "auto")
    base_url = inst.get("base_url", "").strip().rstrip("/")
    try:
        ptype = (
            await detect_provider(base_url)
            if provider == "auto"
            else ProviderType(provider)
        )
    except Exception:
        ptype = ProviderType.OLLAMA  # fallback for label

    return HTMLResponse(_status_pill(health, str(ptype) if health["ok"] else None))


# ---------------------------------------------------------------------------
# Role-first selectors: each role (chat/embed/ocr) picks an active endpoint
# and a model. The model is stored on the active instance's role-specific
# field, which is exactly what get_*_config() resolves at call time.
# ---------------------------------------------------------------------------

_ROLE_FIELD = {"chat": "summary_model", "embed": "embed_model", "ocr": "ocr_model"}
_ROLE_LABEL = {"chat": "Chat", "embed": "Embeddings", "ocr": "OCR"}


def _provider_for(role: str):
    return {"chat": chat_provider, "embed": embed_provider, "ocr": ocr_provider}[role]


def _embed_dim_warning(db) -> str:
    """Rebuild-index warning HTML when the active embed dim != vec0 dim."""
    from app.services.embeddings import verify_vec0_dim

    cfg = get_embed_config(db)
    dim_ok, actual_dim = verify_vec0_dim(db, cfg.embed_dim)
    if dim_ok:
        return ""
    actual = actual_dim or "unknown"
    return (
        '<div class="mt-2 p-3 rounded-xl text-xs border" '
        'style="border-color:var(--color-error);color:var(--color-error)">'
        '<span class="material-symbols-outlined text-[14px] align-middle">warning</span> '
        f"Vector index dim mismatch: index={actual}, config={cfg.embed_dim}. "
        '<button type="button" hx-post="/api/settings/ai/rebuild-index" '
        'hx-target="#reindex-status" hx-swap="innerHTML" '
        'class="underline font-bold ml-1">Rebuild index</button></div>'
    )


@router.post("/role/{role}", response_class=HTMLResponse)
async def set_role(
    role: str,
    request: Request,
    instance_id: str = Form(...),
    model: str = Form(""),
    db: Session = Depends(get_db),
):
    """Set the active endpoint (and optionally the model) for a role.

    Sent by the role card. An empty `model` means the endpoint changed —
    we set active and re-discover that endpoint's models (OOB-swapped into
    the model select). A non-empty `model` means the user picked a model —
    we write it to the active instance's role field and (for embed) re-probe
    the dimension.
    """
    if role not in _ROLE_FIELD:
        return HTMLResponse(_toast(False, "Invalid role"), status_code=400)
    inst = get_instance(db, instance_id)
    if not inst:
        return HTMLResponse(_toast(False, "Instance not found"), status_code=404)

    field = _ROLE_FIELD[role]
    model = model.strip()
    switching = model == ""

    set_active(db, role, instance_id)

    probed_dim = None
    probe_warning = ""
    if not switching:
        inst = {**inst, field: model}
        if role == "embed" and inst.get("embed_model"):
            dim, err = await _probe_embed_dim(inst)
            if dim:
                inst["embed_dim"] = dim
                probed_dim = dim
            elif not inst.get("embed_dim"):
                # Probe failed and no dim is known yet. Persist the model anyway
                # rather than silently discarding the user's pick; embeddings
                # stay gated on a known dim, surfaced as a non-blocking warning.
                probe_warning = (
                    '<div class="mt-1 text-[11px]" style="color:var(--color-error)">'
                    f"Model saved, but embed dimension couldn't be detected: {escape(str(err))}. "
                    "Embeddings stay disabled until the endpoint is reachable — re-select to retry."
                    "</div>"
                )
        save_instance(db, inst)

    provider = _provider_for(role)
    provider.reload_from_db(db)
    health = await provider.probe_health()
    ptype = await provider.get_type()

    warning = (_embed_dim_warning(db) if role == "embed" else "") + probe_warning

    # A model/endpoint change on the embed role changes what get_embed_config
    # resolves to — OOB-refresh the Embedding Index section's Dim/Model so it
    # doesn't go stale until a manual page refresh.
    embed_cfg = get_embed_config(db) if role == "embed" else None

    current_model = inst.get(field, "")
    if switching:
        saved_label = f"Switched to {inst.get('label', 'instance')}"
    else:
        saved_label = f"{current_model or '—'} active for {_ROLE_LABEL[role]}"

    options = ""
    if switching:
        categorized = await _fetch_models(inst)
        options = _model_options(categorized.get(role, []), current_model)

    return templates.TemplateResponse(
        request,
        "partials/settings/_role_card_status.html",
        {
            "role": role,
            "switching": switching,
            "saved_label": saved_label,
            "status_pill": _status_pill(health, str(ptype) if health["ok"] else None),
            "probed_dim": probed_dim,
            "warning": warning,
            "options": options,
            "current_model": current_model,
            "model_dim": inst.get("embed_dim") if role == "embed" else None,
            "embed_cfg": embed_cfg,
        },
    )


@router.get("/role/{role}/models", response_class=HTMLResponse)
async def role_model_options(
    role: str,
    instance_id: str,
    db: Session = Depends(get_db),
):
    """Return the <option> list for one role's model select on a given endpoint."""
    if role not in _ROLE_FIELD:
        return HTMLResponse("<option disabled>Invalid role</option>", status_code=400)
    inst = get_instance(db, instance_id)
    if not inst:
        return HTMLResponse("<option disabled>Instance not found</option>")
    categorized = await _fetch_models(inst)
    return HTMLResponse(
        _model_options(categorized.get(role, []), inst.get(_ROLE_FIELD[role], ""))
    )


# ---------------------------------------------------------------------------
# User context
# ---------------------------------------------------------------------------


@router.post("/user-context", response_class=HTMLResponse)
async def save_user_context(
    user_context: str = Form(""),
    db: Session = Depends(get_db),
):
    set_user_context(db, user_context)
    chat_provider.reload_from_db(db)
    return HTMLResponse(_toast(True, "Context saved"))


# ---------------------------------------------------------------------------
# Index maintenance
# ---------------------------------------------------------------------------


@router.post("/reindex", response_class=HTMLResponse)
@limiter.limit("5/minute")
async def reindex_documents(request: Request, db: Session = Depends(get_db)):
    """Quick reindex using current settings (no DDL change).

    Same async-Celery flow as rebuild_index — just skips the DDL step.
    Both buttons land their state in the singleton reindex_job slot.
    """
    from app.models.database import Document
    from app.services.user_settings_service import (
        get_reindex_job,
        set_reindex_running,
    )
    from app.tasks.dispatch import dispatch_task
    from app.tasks.generate_embedding import reindex_all_embeddings_task

    existing = get_reindex_job(db)
    if existing and existing.get("status") == "running":
        return HTMLResponse(_toast(False, "A reindex is already in flight."))

    embed_provider.reload_from_db(db)
    cfg = get_embed_config(db)
    total = db.query(Document).filter(Document.content.isnot(None)).count()
    set_reindex_running(db, total=total, embed_dim=cfg.embed_dim)
    audit_service.record(db, AuditEventType.MAINTENANCE_REINDEX_DOCUMENTS)
    db.commit()

    dispatch_task(reindex_all_embeddings_task)

    return templates.TemplateResponse(
        request,
        "partials/settings/_reindex_running.html",
        {"job": get_reindex_job(db)},
    )


@router.post("/rebuild-index", response_class=HTMLResponse)
@limiter.limit("5/minute")
async def rebuild_index(
    request: Request,
    db: Session = Depends(get_db),
):
    """Drop+recreate the vec0 table, then dispatch the embedding reindex
    as a Celery task. The response is a polling fragment that the browser
    refreshes against /rebuild-index/status every 4s.
    """
    from app.models.database import Document
    from app.services.user_settings_service import (
        get_reindex_job,
        set_reindex_running,
    )
    from app.tasks.dispatch import dispatch_task
    from app.tasks.generate_embedding import reindex_all_embeddings_task

    cfg = get_embed_config(db)
    embed_dim = cfg.embed_dim

    if not (64 <= embed_dim <= 4096):
        return HTMLResponse(
            _toast(False, f"embed_dim={embed_dim} out of range 64–4096"),
            status_code=400,
        )

    # Refuse concurrent reindex (singleton job).
    existing = get_reindex_job(db)
    if existing and existing.get("status") == "running":
        return HTMLResponse(_toast(False, "A reindex is already in flight."))

    embed_provider.reload_from_db(db)

    try:
        db.execute(text("DROP TABLE IF EXISTS document_chunk_vectors"))
        db.execute(
            text(
                f"CREATE VIRTUAL TABLE document_chunk_vectors USING vec0("
                f"chunk_id INTEGER PRIMARY KEY, embedding float[{embed_dim}])",
            ),
        )
        # document_chunks rows are about to be orphaned (their vec0 rows are
        # gone and a full reindex is starting) — clear them so stale chunk
        # text doesn't linger indefinitely.
        db.execute(text("DELETE FROM document_chunks"))
        audit_service.record(db, AuditEventType.MAINTENANCE_REBUILD_INDEX)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to recreate document_chunk_vectors: {e}")
        return HTMLResponse(_toast(False, f"DDL failed: {e}"))

    total = db.query(Document).filter(Document.content.isnot(None)).count()
    set_reindex_running(db, total=total, embed_dim=embed_dim)
    db.commit()

    dispatch_task(reindex_all_embeddings_task)

    return templates.TemplateResponse(
        request,
        "partials/settings/_reindex_running.html",
        {"job": get_reindex_job(db)},
    )


@router.get("/rebuild-index/status", response_class=HTMLResponse)
async def rebuild_index_status(request: Request, db: Session = Depends(get_db)):
    """Polling endpoint for the reindex progress UI. Returns the running
    fragment while in flight, the result fragment when done or failed.
    """
    from app.services.user_settings_service import get_reindex_job

    job = get_reindex_job(db)
    template = (
        "partials/settings/_reindex_running.html"
        if job and job.get("status") == "running"
        else "partials/settings/_reindex_result.html"
    )
    return templates.TemplateResponse(request, template, {"job": job})


# ---------------------------------------------------------------------------
# Debug log redaction
# ---------------------------------------------------------------------------


@router.post("/debug-redact")
async def set_debug_redact(
    request: Request,
    enabled: str = Form(...),
    db: Session = Depends(get_db),
):
    from app.services.user_settings_service import set_ai_debug_redact

    set_ai_debug_redact(db, enabled.lower() == "true")
    from fastapi.responses import Response

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Extraction engine toggle (Chandra vs Docling for PDFs)
# ---------------------------------------------------------------------------


@router.post("/extraction-engine", response_class=HTMLResponse)
async def set_extraction_engine_route(
    engine: str = Form(...),
    db: Session = Depends(get_db),
):
    """Persist the PDF extraction engine choice (chandra | docling)."""
    from app.services.user_settings_service import (
        VALID_EXTRACTION_ENGINES,
        set_extraction_engine,
    )

    if engine not in VALID_EXTRACTION_ENGINES:
        return HTMLResponse(_toast(False, f"Invalid engine: {engine}"), status_code=400)
    set_extraction_engine(db, engine)
    return HTMLResponse(_toast(True, f"Default engine: {engine}"))


@router.post("/worker-concurrency", response_class=HTMLResponse)
async def set_worker_concurrency_route(
    concurrency: int = Form(...),
    db: Session = Depends(get_db),
):
    """Persist the `ai` worker concurrency and apply it live (no restart)."""
    from app.services.user_settings_service import set_worker_concurrency
    from app.services.worker_control import apply_ai_concurrency

    try:
        set_worker_concurrency(db, concurrency)
    except ValueError as e:
        return HTMLResponse(_toast(False, str(e)), status_code=400)

    res = apply_ai_concurrency(concurrency)
    msg = (
        f"AI concurrency → {concurrency} (applied live)"
        if res["live"]
        else f"Saved ({concurrency}); applies on next worker start"
    )
    return HTMLResponse(_toast(True, msg))


@router.post("/ocr-concurrency", response_class=HTMLResponse)
async def set_ocr_concurrency_route(
    concurrency: int = Form(...),
    db: Session = Depends(get_db),
):
    """Persist OCR-slot concurrency and apply it live (no restart).

    Resizes the `ingest` worker's prefork pool AND republishes the limit
    used by the per-page `ocr_slots.ocr_slot()` semaphore, so both halves of
    the cap (documents-in-flight and total concurrent OCR-model calls) move
    together.
    """
    from app.services.ocr_slots import set_limit as set_ocr_slot_limit
    from app.services.user_settings_service import set_ocr_concurrency
    from app.services.worker_control import apply_ocr_concurrency

    try:
        set_ocr_concurrency(db, concurrency)
    except ValueError as e:
        return HTMLResponse(_toast(False, str(e)), status_code=400)

    set_ocr_slot_limit(concurrency)
    res = apply_ocr_concurrency(concurrency)
    msg = (
        f"OCR concurrency → {concurrency} (applied live)"
        if res["live"]
        else f"Saved ({concurrency}); applies on next worker start"
    )
    return HTMLResponse(_toast(True, msg))


# ---------------------------------------------------------------------------
# Debug log browser
# ---------------------------------------------------------------------------

_AI_DEBUG_ROOT = (DATA_DIR / "ai_debug").resolve()


def _tail_jsonl(path, limit: int) -> list[dict]:
    """Return the last `limit` parsed JSON lines from `path`, newest first.

    Small enough to read fully for now (the index file is line-per-call and
    grows slowly). Switching to a chunked reverse-read can wait until the
    file is many MB.
    """
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(reversed(rows[-limit:]))


@router.get("/debug-logs", response_class=HTMLResponse)
async def list_debug_logs(request: Request, limit: int = 50):
    """Render the most recent AI calls from data/ai_debug/runs.jsonl."""
    index_path = _AI_DEBUG_ROOT / "runs.jsonl"
    rows = _tail_jsonl(index_path, limit)
    return templates.TemplateResponse(
        request,
        "partials/settings/_debug_log_list.html",
        {"rows": rows, "log_root": str(_AI_DEBUG_ROOT)},
    )


@router.get("/debug-logs/view", response_class=HTMLResponse)
async def view_debug_log(request: Request, path: str):
    """Stream a single .md file from data/ai_debug/ for in-UI inspection.

    Path-safety: resolve the requested path under _AI_DEBUG_ROOT and refuse
    anything that escapes the directory or isn't a .md file. Returns 422 on
    any guard violation.
    """
    if "\0" in path or ".." in path.split("/"):
        raise HTTPException(status_code=422, detail="Invalid path")
    candidate = (_AI_DEBUG_ROOT / path).resolve()
    try:
        candidate.relative_to(_AI_DEBUG_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Path escapes debug root") from exc
    if candidate.suffix != ".md" or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")
    body = candidate.read_text(encoding="utf-8", errors="replace")
    return templates.TemplateResponse(
        request,
        "partials/settings/_debug_log_view.html",
        {"path": path, "body": body},
    )

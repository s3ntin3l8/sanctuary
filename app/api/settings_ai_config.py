"""AI configuration settings endpoints: instance CRUD, model discovery, index rebuild."""

import json
import logging
from html import escape

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
    return "".join(
        f'<option value="{escape(m, quote=True)}" {"selected" if m == selected else ""}>'
        f"{escape(m)}</option>"
        for m in models
    )


# ---------------------------------------------------------------------------
# Instance CRUD
# ---------------------------------------------------------------------------


@router.post("/instances", response_class=HTMLResponse)
async def create_instance(
    request: Request,
    label: str = Form("New Instance"),
    base_url: str = Form("http://127.0.0.1:11434"),
    provider: str = Form("auto"),
    api_key: str = Form("not-needed"),
    summary_model: str = Form(""),
    embed_model: str = Form(""),
    ocr_model: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.ai_config import _make_id

    inst_id = _make_id()
    instance = {
        "id": inst_id,
        "label": label.strip() or "New Instance",
        "base_url": base_url.strip().rstrip("/"),
        "provider": provider.strip(),
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
    models = (
        await _fetch_models(instance) if health["ok"] else {"chat": [], "embed": []}
    )

    return templates.TemplateResponse(
        request,
        "partials/settings/_ai_instance_row.html",
        {
            "inst": instance,
            "health": health,
            "models": models,
            "expanded": True,
        },
        headers={"HX-Reswap": "beforeend", "HX-Retarget": "#ai-instances"},
    )


@router.post("/instances/{instance_id}", response_class=HTMLResponse)
async def save_instance_route(
    instance_id: str,
    request: Request,
    label: str = Form(""),
    base_url: str = Form(""),
    provider: str = Form("auto"),
    api_key: str = Form(""),
    summary_model: str = Form(""),
    embed_model: str = Form(""),
    ocr_model: str = Form(""),
    db: Session = Depends(get_db),
):
    existing = get_instance(db, instance_id)
    if not existing:
        return HTMLResponse(_toast(False, "Instance not found"), status_code=404)

    new_embed_model = embed_model.strip() or existing.get("embed_model", "")
    instance = {
        "id": instance_id,
        "label": label.strip() or existing.get("label", "Instance"),
        "base_url": (base_url.strip() or existing.get("base_url", "")).rstrip("/"),
        "provider": provider.strip() or existing.get("provider", "auto"),
        "api_key": api_key.strip() or existing.get("api_key", "not-needed"),
        "summary_model": summary_model.strip() or existing.get("summary_model", ""),
        "embed_model": new_embed_model,
        "embed_dim": existing.get("embed_dim"),
        "ocr_model": ocr_model.strip() or existing.get("ocr_model", ""),
    }

    # Always probe when embed_model is set — catches stale dims from before auto-detect.
    # On failure: hard-fail only if we have no stored dim at all; otherwise keep existing.
    if new_embed_model:
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
    if ai.get("active_embed_id") == instance_id and instance.get("embed_dim"):
        dim_ok, actual_dim = verify_vec0_dim(db, instance["embed_dim"])
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
            "health": {"ok": None, "detail": "Saved"},
            "models": [],
            "expanded": True,
            "dim_warning": dim_warning,
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

    headers = {}
    if health["ok"]:
        headers["HX-Trigger"] = f"refresh-models-{instance_id}"

    return HTMLResponse(
        _status_pill(health, str(ptype) if health["ok"] else None),
        headers=headers,
    )


@router.get("/instances/{instance_id}/models", response_class=HTMLResponse)
async def instance_models(
    instance_id: str,
    db: Session = Depends(get_db),
):
    """Return HTMX Out-of-Band swaps that populate model <select> elements."""
    inst = get_instance(db, instance_id)
    if not inst:
        return HTMLResponse("<option disabled>Instance not found</option>")

    categorized = await _fetch_models(inst)
    safe_id = escape(instance_id, quote=True)

    chat_models = categorized.get("chat", [])
    embed_models = categorized.get("embed", [])
    ocr_models = categorized.get("ocr", [])

    # If categorized lists are empty, provide fallback options
    summary_opts = (
        _model_options(chat_models, inst.get("summary_model", ""))
        if chat_models
        else f'<option value="{escape(inst.get("summary_model", ""), quote=True)}" selected>{escape(inst.get("summary_model", "") or "No chat models found")}</option>'
    )
    embed_opts = (
        _model_options(embed_models, inst.get("embed_model", ""))
        if embed_models
        else f'<option value="{escape(inst.get("embed_model", ""), quote=True)}" selected>{escape(inst.get("embed_model", "") or "No embedding models found")}</option>'
    )
    ocr_opts = (
        _model_options(ocr_models, inst.get("ocr_model", ""))
        if ocr_models
        else f'<option value="{escape(inst.get("ocr_model", ""), quote=True)}" selected>{escape(inst.get("ocr_model", "") or "No OCR models found")}</option>'
    )

    # Return all three selects with OOB swap
    res = [
        f'<select id="summary_model_{safe_id}" name="summary_model" hx-swap-oob="innerHTML">{summary_opts}</select>',
        f'<select id="embed_model_{safe_id}" name="embed_model" hx-swap-oob="innerHTML">{embed_opts}</select>',
        f'<select id="ocr_model_{safe_id}" name="ocr_model" hx-swap-oob="innerHTML">{ocr_opts}</select>',
    ]

    return HTMLResponse("".join(res))


# ---------------------------------------------------------------------------
# Active instance selectors
# ---------------------------------------------------------------------------


@router.post("/active", response_class=HTMLResponse)
async def set_active_instance(
    role: str = Form(...),
    instance_id: str = Form(...),
    db: Session = Depends(get_db),
):
    if role not in ("chat", "embed", "ocr"):
        return HTMLResponse(_toast(False, "Invalid role"), status_code=400)

    inst = get_instance(db, instance_id)
    if not inst:
        return HTMLResponse(_toast(False, "Instance not found"), status_code=404)

    set_active(db, role, instance_id)

    if role == "chat":
        chat_provider.reload_from_db(db)
        health = await chat_provider.probe_health()
        ptype = await chat_provider.get_type()
        return HTMLResponse(_status_pill(health, str(ptype) if health["ok"] else None))
    if role == "ocr":
        ocr_provider.reload_from_db(db)
        health = await ocr_provider.probe_health()
        ptype = await ocr_provider.get_type()
        return HTMLResponse(_status_pill(health, str(ptype) if health["ok"] else None))
    embed_provider.reload_from_db(db)
    health = await embed_provider.probe_health()
    ptype = await embed_provider.get_type()

    # Warn if embed dim mismatch
    from app.services.embeddings import verify_vec0_dim

    cfg = get_embed_config(db)
    dim_ok, actual_dim = verify_vec0_dim(db, cfg.embed_dim)
    extra = ""
    if not dim_ok:
        actual = actual_dim or "unknown"
        extra = (
            f'<div class="mt-2 p-3 rounded-xl text-xs border" '
            f'style="border-color:var(--color-error);color:var(--color-error)">'
            f'<span class="material-symbols-outlined text-[14px] align-middle">warning</span> '
            f"Vector index dim mismatch: index={actual}, new config={cfg.embed_dim}. "
            f'<button type="button" '
            f'hx-post="/api/settings/ai/rebuild-index" '
            f'hx-target="#rebuild-result" hx-swap="innerHTML" '
            f'class="underline font-bold ml-1">Rebuild index</button>'
            f"</div>"
        )

    return HTMLResponse(
        _status_pill(health, str(ptype) if health["ok"] else None) + extra,
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
        db.execute(text("DROP TABLE IF EXISTS document_vectors"))
        db.execute(
            text(
                f"CREATE VIRTUAL TABLE document_vectors USING vec0("
                f"document_id INTEGER PRIMARY KEY, embedding float[{embed_dim}])",
            ),
        )
        audit_service.record(db, AuditEventType.MAINTENANCE_REBUILD_INDEX)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to recreate document_vectors: {e}")
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

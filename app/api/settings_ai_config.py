"""AI configuration settings endpoints: instance CRUD, model discovery, index rebuild."""

import logging
from html import escape

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import templates
from app.dependencies import get_db
from app.services.ai_config import (
    delete_instance,
    get_embed_config,
    get_instance,
    save_instance,
    set_active,
    set_user_context,
)
from app.services.ai_provider import chat_provider, embed_provider
from app.services.embeddings import reindex_all_docs

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


async def _fetch_models(inst: dict) -> list[str]:
    """Fetch available models from a specific instance."""
    base_url = inst.get("base_url", "").strip().rstrip("/")
    provider = inst.get("provider", "auto")
    api_key = inst.get("api_key", "not-needed")

    from app.services.ai_provider import ProviderType, detect_provider

    if provider == "auto":
        ptype = await detect_provider(base_url)
    else:
        ptype = ProviderType(provider)

    models: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            if ptype == ProviderType.OLLAMA:
                resp = await client.get(f"{base_url}/api/tags")
                if resp.status_code == 200:
                    models = [m["name"] for m in resp.json().get("models", [])]
            else:
                headers = (
                    {"Authorization": f"Bearer {api_key}"}
                    if api_key != "not-needed"
                    else {}
                )
                resp = await client.get(f"{base_url}/v1/models", headers=headers)
                if resp.status_code == 200:
                    models = [m["id"] for m in resp.json().get("data", [])]
    except Exception as e:
        logger.warning(f"Model discovery failed for {base_url}: {e}")
    return models


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
    embed_dim: int = Form(768),
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
        "embed_dim": embed_dim,
    }
    save_instance(db, instance)
    health = await _probe_instance(instance)
    models = await _fetch_models(instance) if health["ok"] else []

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
    embed_dim: int = Form(768),
    db: Session = Depends(get_db),
):
    existing = get_instance(db, instance_id)
    if not existing:
        return HTMLResponse(_toast(False, "Instance not found"), status_code=404)

    instance = {
        "id": instance_id,
        "label": label.strip() or existing.get("label", "Instance"),
        "base_url": (base_url.strip() or existing.get("base_url", "")).rstrip("/"),
        "provider": provider.strip() or existing.get("provider", "auto"),
        "api_key": api_key.strip() or existing.get("api_key", "not-needed"),
        "summary_model": summary_model.strip() or existing.get("summary_model", ""),
        "embed_model": embed_model.strip() or existing.get("embed_model", ""),
        "embed_dim": embed_dim,
    }
    save_instance(db, instance)
    chat_provider.reload_from_db(db)
    embed_provider.reload_from_db(db)

    health = await _probe_instance(instance)
    models = await _fetch_models(instance) if health["ok"] else []

    return templates.TemplateResponse(
        request,
        "partials/settings/_ai_instance_row.html",
        {
            "inst": instance,
            "health": health,
            "models": models,
            "expanded": True,
        },
    )


@router.delete("/instances/{instance_id}", response_class=HTMLResponse)
async def delete_instance_route(
    instance_id: str,
    db: Session = Depends(get_db),
):
    from app.services.ai_config import _ensure_migrated, _get_ai_section

    _ensure_migrated(db)
    ai = _get_ai_section(db)
    if (
        ai.get("active_chat_id") == instance_id
        or ai.get("active_embed_id") == instance_id
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
async def test_instance(
    instance_id: str,
    db: Session = Depends(get_db),
):
    inst = get_instance(db, instance_id)
    if not inst:
        return HTMLResponse(_toast(False, "Instance not found"), status_code=404)
    health = await _probe_instance(inst)
    from app.services.ai_provider import ProviderType, detect_provider

    provider = inst.get("provider", "auto")
    base_url = inst.get("base_url", "").strip().rstrip("/")
    ptype = (
        await detect_provider(base_url)
        if provider == "auto"
        else ProviderType(provider)
    )
    return HTMLResponse(_status_pill(health, str(ptype) if health["ok"] else None))


@router.get("/instances/{instance_id}/models", response_class=HTMLResponse)
async def instance_models(
    instance_id: str,
    db: Session = Depends(get_db),
):
    """Return JS snippet that populates model <select> elements for a specific instance row."""
    inst = get_instance(db, instance_id)
    if not inst:
        return HTMLResponse("<option disabled>Instance not found</option>")

    models = await _fetch_models(inst)
    if not models:
        return HTMLResponse(
            f"<script>"
            f"(function(){{"
            f'var s=document.getElementById("summary_model_{instance_id}");'
            f'var e=document.getElementById("embed_model_{instance_id}");'
            f"if(s){{s.innerHTML='<option disabled>No models found</option>';}} "
            f"if(e){{e.innerHTML='<option disabled>No models found</option>';}} "
            f"}})();"
            f"</script>",
        )

    summary_opts = _model_options(models, inst.get("summary_model", ""))
    embed_opts = _model_options(models, inst.get("embed_model", ""))

    return HTMLResponse(
        f"<script>"
        f"(function(){{"
        f'var s=document.getElementById("summary_model_{escape(instance_id, quote=True)}");'
        f'var e=document.getElementById("embed_model_{escape(instance_id, quote=True)}");'
        f"if(s){{s.innerHTML={summary_opts!r};}} "
        f"if(e){{e.innerHTML={embed_opts!r};}} "
        f"}})();"
        f"</script>",
    )


# ---------------------------------------------------------------------------
# Active instance selectors
# ---------------------------------------------------------------------------


@router.post("/active", response_class=HTMLResponse)
async def set_active_instance(
    role: str = Form(...),
    instance_id: str = Form(...),
    db: Session = Depends(get_db),
):
    if role not in ("chat", "embed"):
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
            f'hx-include="[name=embed_dim],[name=embed_model]" '
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
async def reindex_documents(db: Session = Depends(get_db)):
    """Quick reindex using current settings (no DDL change)."""
    embed_provider.reload_from_db(db)
    result = await reindex_all_docs(db)
    fail_note = f" ({result['failed']} failed)" if result["failed"] else ""
    return HTMLResponse(
        f'<span class="text-xs" style="color:var(--color-on-surface-variant)">'
        f"Reindexed {result['reindexed']}/{result['total']} documents{fail_note}"
        f"</span>",
    )


@router.post("/rebuild-index", response_class=HTMLResponse)
async def rebuild_index(
    embed_model: str = Form(""),
    embed_dim: int = Form(768),
    db: Session = Depends(get_db),
):
    if not (64 <= embed_dim <= 4096):
        return HTMLResponse(
            _toast(False, "embed_dim must be between 64 and 4096"),
            status_code=400,
        )

    # Persist embed_dim and embed_model to the active embed instance
    from app.services.ai_config import _ensure_migrated, _get_ai_section, get_instance

    _ensure_migrated(db)
    ai = _get_ai_section(db)
    active_embed_id = ai.get("active_embed_id")
    if active_embed_id:
        inst = get_instance(db, active_embed_id)
        if inst:
            updated = dict(inst)
            if embed_model:
                updated["embed_model"] = embed_model
            updated["embed_dim"] = embed_dim
            save_instance(db, updated)

    embed_provider.reload_from_db(db)

    try:
        db.execute(text("DROP TABLE IF EXISTS document_vectors"))
        db.execute(
            text(
                f"CREATE VIRTUAL TABLE document_vectors USING vec0("
                f"document_id INTEGER PRIMARY KEY, embedding float[{embed_dim}])",
            ),
        )
        db.commit()
    except Exception as e:
        logger.error(f"Failed to recreate document_vectors: {e}")
        return HTMLResponse(_toast(False, f"DDL failed: {e}"))

    try:
        result = await reindex_all_docs(db)
        fail_note = f" ({result['failed']} failed)" if result["failed"] else ""
        return HTMLResponse(
            _toast(
                True,
                f"Rebuilt: {result['reindexed']}/{result['total']} documents indexed{fail_note}",
            ),
        )
    except Exception as e:
        logger.error(f"Reindex failed: {e}")
        return HTMLResponse(_toast(False, f"Reindex failed: {e}"))

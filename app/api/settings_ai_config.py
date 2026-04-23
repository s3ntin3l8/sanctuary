"""AI configuration settings endpoints: config save, model discovery, index rebuild."""

import logging

import httpx
from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.dependencies import get_db
from app.services.ai_config import get_effective_config
from app.services.ai_provider import ai_provider
from app.services.embeddings import reindex_all_docs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/ai", tags=["settings"])


def _toast(ok: bool, message: str) -> str:
    color = "var(--color-primary)" if ok else "var(--color-error)"
    symbol = "check_circle" if ok else "error"
    return (
        f'<span class="inline-flex items-center gap-1.5 text-xs">'
        f'<span class="material-symbols-outlined text-[14px]" style="color:{color}">{symbol}</span>'
        f'<span style="color:{color}">{message}</span>'
        f"</span>"
    )


@router.post("/reindex", response_class=HTMLResponse)
async def reindex_documents(db: Session = Depends(get_db)):
    """Quick reindex using current settings (no DDL change)."""
    ai_provider.reload_from_db(db)
    result = await reindex_all_docs(db)
    fail_note = f" ({result['failed']} failed)" if result["failed"] else ""
    return HTMLResponse(
        f'<span class="text-xs" style="color:var(--color-on-surface-variant)">'
        f"Reindexed {result['reindexed']}/{result['total']} documents{fail_note}"
        f"</span>"
    )


@router.post("/test", response_class=HTMLResponse)
async def test_ai_connection(db: Session = Depends(get_db)):
    ai_provider.reload_from_db(db)
    result = await ai_provider.probe_health()
    color = "var(--color-primary)" if result["ok"] else "var(--color-error)"
    symbol = "check_circle" if result["ok"] else "error"
    return HTMLResponse(
        f'<span class="inline-flex items-center gap-1.5 text-xs">'
        f'<span class="material-symbols-outlined text-[14px]" style="color:{color}">{symbol}</span>'
        f'<span style="color:{color}">{result["detail"]}</span>'
        f"</span>",
        headers={"HX-Trigger": "refresh-models"},
    )


@router.get("/models", response_class=HTMLResponse)
async def list_models(db: Session = Depends(get_db)):
    """Probe the configured provider and return <option> tags for both model selects."""
    cfg = get_effective_config(db)
    ai_provider.reload_from_db(db)

    models: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            ptype = await ai_provider.get_type()
            from app.services.ai_provider import ProviderType

            if ptype == ProviderType.OLLAMA:
                resp = await client.get(f"{cfg.base_url}/api/tags")
                if resp.status_code == 200:
                    models = [m["name"] for m in resp.json().get("models", [])]
            else:
                resp = await client.get(
                    f"{cfg.base_url}/v1/models",
                    headers={"Authorization": f"Bearer {cfg.api_key}"}
                    if cfg.api_key != "not-needed"
                    else {},
                )
                if resp.status_code == 200:
                    models = [m["id"] for m in resp.json().get("data", [])]
    except Exception as e:
        logger.warning(f"Model discovery failed: {e}")

    if not models:
        return HTMLResponse(
            "<option disabled selected>No models found — check connection</option>"
        )

    def options(selected: str, tag_id: str) -> str:
        return "".join(
            f'<option value="{m}" {"selected" if m == selected else ""}>{m}</option>'
            for m in models
        )

    summary_opts = options(cfg.summary_model, "summary")
    embed_opts = options(cfg.embed_model, "embed")

    return HTMLResponse(
        f'<div id="summary-model-options" style="display:none">{summary_opts}</div>'
        f'<div id="embed-model-options" style="display:none">{embed_opts}</div>'
        f"<script>"
        f"(function(){{"
        f'var s=document.getElementById("summary_model");'
        f'if(s){{s.innerHTML=document.getElementById("summary-model-options").innerHTML;}}'
        f'var e=document.getElementById("embed_model");'
        f'if(e){{e.innerHTML=document.getElementById("embed-model-options").innerHTML;}}'
        f"}})();"
        f"</script>"
    )


@router.post("/config", response_class=HTMLResponse)
async def save_ai_config(
    base_url: str = Form(""),
    provider: str = Form("auto"),
    api_key: str = Form(""),
    summary_model: str = Form(""),
    embed_model: str = Form(""),
    user_context: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.models.database import UserSettings

    settings = (
        db.query(UserSettings).filter(UserSettings.user_id == "single_user").first()
    )
    if not settings:
        settings = UserSettings(user_id="single_user")
        db.add(settings)

    data = dict(settings.settings_json or {})
    ai = dict(data.get("ai", {}))
    if base_url:
        ai["base_url"] = base_url.rstrip("/")
    if provider:
        ai["provider"] = provider
    if api_key:
        ai["api_key"] = api_key
    if summary_model:
        ai["summary_model"] = summary_model
    if embed_model:
        ai["embed_model"] = embed_model
    ai["user_context"] = user_context
    data["ai"] = ai
    settings.settings_json = data
    db.commit()

    ai_provider.reload_from_db(db)
    return HTMLResponse(
        _toast(True, "Configuration saved"), headers={"HX-Trigger": "refresh-models"}
    )


@router.post("/rebuild-index", response_class=HTMLResponse)
async def rebuild_index(
    embed_model: str = Form(""),
    embed_dim: int = Form(768),
    db: Session = Depends(get_db),
):
    from app.models.database import UserSettings

    settings = (
        db.query(UserSettings).filter(UserSettings.user_id == "single_user").first()
    )
    if not settings:
        settings = UserSettings(user_id="single_user")
        db.add(settings)

    data = dict(settings.settings_json or {})
    ai = dict(data.get("ai", {}))
    if embed_model:
        ai["embed_model"] = embed_model
    ai["embed_dim"] = embed_dim
    data["ai"] = ai
    settings.settings_json = data
    db.commit()

    ai_provider.reload_from_db(db)

    try:
        db.execute(text("DROP TABLE IF EXISTS document_vectors"))
        db.execute(
            text(
                f"CREATE VIRTUAL TABLE document_vectors USING vec0("
                f"document_id INTEGER PRIMARY KEY, embedding float[{embed_dim}])"
            )
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
            )
        )
    except Exception as e:
        logger.error(f"Reindex failed: {e}")
        return HTMLResponse(_toast(False, f"Reindex failed: {e}"))

"""AI-settings helpers shared by the settings API: embed-dim probing, model
discovery/categorisation, role resolution and the AI debug-log index."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from app.config import DATA_DIR
from app.services.ai_config import _resolve_active
from app.services.ai_provider import chat_provider, embed_provider, ocr_provider

logger = logging.getLogger(__name__)

ROLES = ("chat", "embed", "ocr")
ROLE_FIELD = {"chat": "summary_model", "embed": "embed_model", "ocr": "ocr_model"}
ROLE_LABEL = {"chat": "Chat", "embed": "Embeddings", "ocr": "OCR"}
ROLE_HINT = {
    "chat": "Powers case briefs, chat answers, and document summaries.",
    "embed": "Powers semantic search and the vector index.",
    "ocr": "Reads scanned or stamped PDFs when the Chandra engine is active.",
}

AI_DEBUG_ROOT = (DATA_DIR / "ai_debug").resolve()


def provider_for(role: str):
    return {"chat": chat_provider, "embed": embed_provider, "ocr": ocr_provider}[role]


async def probe_instance(inst: dict) -> dict:
    """Probe health for a specific instance config dict."""
    return await chat_provider.probe_health(config=inst)


async def probe_embed_dim(inst: dict) -> tuple[int | None, str | None]:
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


def categorize_models(models: list[str]) -> dict[str, list[str]]:
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


async def fetch_models(inst: dict) -> dict[str, list[str]]:
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
        return categorize_models([])

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
                    if api_key != "not-needed"  # pragma: allowlist secret
                    else {}
                )
                resp = await client.get(f"{base_url}/v1/models", headers=headers)
                if resp.status_code == 200:
                    all_models = [m["id"] for m in resp.json().get("data", [])]
    except Exception as e:
        logger.warning(f"Model discovery failed for {base_url}: {e}")

    return categorize_models(all_models)


def resolve_roles(db) -> dict[str, dict]:
    """The instance each role resolves to (``{}`` when none is configured)."""
    return {role: _resolve_active(db, role) for role in ROLES}


async def probe_role_health(db) -> dict[str, dict]:
    """Live health per role, probing each distinct active instance once."""
    resolved = resolve_roles(db)
    inst_by_id = {inst["id"]: inst for inst in resolved.values() if inst.get("id")}
    ids = list(inst_by_id)
    results = await asyncio.gather(*[probe_instance(inst_by_id[i]) for i in ids])
    health_by_id = dict(zip(ids, results, strict=True))
    out = {}
    for role, inst in resolved.items():
        aid = inst.get("id")
        out[role] = (
            health_by_id[aid]
            if aid
            else {"ok": False, "detail": "No endpoint configured"}
        )
    return out


def tail_jsonl(path, limit: int) -> list[dict]:
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


def read_debug_log(path: str) -> str | None:
    """Return a .md debug log under AI_DEBUG_ROOT, or None if it does not exist.

    Raises ValueError for any path that escapes the debug root.
    """
    if "\0" in path or ".." in path.split("/"):
        raise ValueError("Invalid path")
    candidate = (AI_DEBUG_ROOT / path).resolve()
    try:
        candidate.relative_to(AI_DEBUG_ROOT)
    except ValueError as exc:
        raise ValueError("Path escapes debug root") from exc
    if candidate.suffix != ".md" or not candidate.is_file():
        return None
    return candidate.read_text(encoding="utf-8", errors="replace")


def debug_log_path(row: dict[str, Any]) -> str | None:
    """Relative .md path for a runs.jsonl row, mirroring how the writer lays files out."""
    stage = row.get("stage")
    if stage in ("ocr", "embed", "slice"):
        return None
    kind, scope_id, batch_id = row.get("kind"), row.get("scope_id"), row.get("batch_id")
    if batch_id and kind in ("doc", "batch"):
        return f"ib-{int(batch_id):04d}/{kind}_{scope_id}.md"
    if kind == "case":
        return f"case_{scope_id}.md"
    return f"unbatched/{kind or 'misc'}_{scope_id}.md"

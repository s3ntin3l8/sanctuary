"""AI-settings helpers shared by the settings API: embed-dim probing, model
discovery/categorisation, role resolution and the AI debug-log index."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from app.config import DATA_DIR
from app.core.safe_error import describe_exception
from app.services import ai_debug_store
from app.services.ai_config import _resolve_active
from app.services.ai_provider import (
    chat_provider,
    embed_provider,
    models_in,
    models_request,
    normalize_base_url,
    ocr_provider,
)

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
    base_url = normalize_base_url(inst.get("base_url", ""))
    provider = inst.get("provider", "auto")
    api_key = inst.get("api_key", "not-needed")

    from app.services.ai_provider import ProviderType, detect_provider

    try:
        ptype = (
            await detect_provider(base_url, api_key)
            if provider == "auto"
            else ProviderType(provider)
        )
    except RuntimeError, Exception:
        return categorize_models([])

    all_models: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            url, headers = models_request(ptype, base_url, api_key)
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                all_models = models_in(ptype, resp.json())
    except Exception as e:
        logger.warning(
            f"Model discovery failed for {base_url}: {describe_exception(e)}"
        )

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


def _read_runs(path) -> list[dict]:
    """Every parsed line of the runs index, oldest first; unreadable lines are skipped.

    Small enough to read fully (one line per call, pruned after 30 days).
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
    return rows


def query_runs(
    path,
    *,
    limit: int,
    kind: str | None = None,
    stage: str | None = None,
    model: str | None = None,
    status: str | None = None,
    doc_id: int | None = None,
    batch_id: int | None = None,
    q: str | None = None,
) -> tuple[list[dict], dict[str, list[str]]]:
    """The newest ``limit`` runs matching every given filter, newest first.

    ``status="error"`` matches anything that isn't ``ok``. ``q`` is a
    case-insensitive substring of the scope id or the error. The facets are the
    distinct kind/stage/model values over the whole file, so the filter
    dropdowns don't shrink as filters apply.
    """
    rows = _read_runs(path)
    facets = {
        key: sorted({str(r[key]) for r in rows if r.get(key)})
        for key in ("kind", "stage", "model")
    }
    needle = q.lower() if q else None

    def keep(r: dict) -> bool:
        if kind and r.get("kind") != kind:
            return False
        if stage and r.get("stage") != stage:
            return False
        if model and r.get("model") != model:
            return False
        if status == "ok" and r.get("status") != "ok":
            return False
        if status == "error" and r.get("status") == "ok":
            return False
        if doc_id is not None and r.get("doc_id") != doc_id:
            return False
        if batch_id is not None and r.get("batch_id") != batch_id:
            return False
        haystack = f"{r.get('scope_id') or ''} {r.get('error') or ''}".lower()
        return not needle or needle in haystack

    matched = [r for r in rows if keep(r)]
    return list(reversed(matched[-limit:])), facets


def extract_call_section(body: str, stage: str, ts: str) -> str | None:
    """The one ``# call:`` block of a per-document log matching ``stage`` and ``ts``.

    Blocks are separated by a rule line of ``═``; each starts with
    ``# call: <label> | stage=<stage> | ts=<ts> | ...``. Returns None when no
    block matches (the caller falls back to the whole file).
    """
    marker = f"| stage={stage} | ts={ts} |"
    for block in re.split(r"^═{16,}\n", body, flags=re.MULTILINE):
        if marker in block.split("\n", 1)[0]:
            return block.rstrip() + "\n"
    return None


def read_debug_log(path: str) -> str | None:
    """Return a .md debug log under AI_DEBUG_ROOT, or None if it does not exist.

    Raises ValueError for any path that escapes the debug root.
    """
    if "\0" in path:
        raise ValueError("Invalid path")
    root = str(AI_DEBUG_ROOT)
    normalized = os.path.normpath(os.path.join(root, path))
    if not normalized.startswith(root + os.sep):
        raise ValueError("Path escapes debug root")
    candidate = Path(normalized)
    if candidate.suffix != ".md" or not candidate.is_file():
        return None
    return ai_debug_store.read_text(candidate)


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

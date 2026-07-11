import logging
import time

import httpx

from app.config import SessionLocal
from app.models.database import Document
from app.services.ai_config import get_embed_config
from app.services.ai_inflight import track_ai_call_async
from app.services.ai_run_index import record_run
from app.services.model_gate import model_gate

logger = logging.getLogger(__name__)

# nomic-embed-text:v1.5 has an 8192-token context window; ~3 chars/token for
# German legal text. Each Docling/OCR chunk is already section-sized, so this
# is a per-chunk safety cap, not sized for whole-document context (contrast
# the old whole-document _EMBED_MAX_CHARS=22000 budget this replaces).
_CHUNK_EMBED_MAX_CHARS = 4000

# A document's matching passage may not be its only high-ranked chunk, and
# several chunks from the same document can appear before a different
# document's best chunk — oversample so grouping/deduping by document still
# yields k distinct documents.
_CHUNK_RETRIEVAL_OVERSAMPLE = 5

from app.services.ai_provider import embed_provider


def _serialize(vec: list[float]) -> bytes:
    """Convert a float list to sqlite-vec f32 blob."""
    from sqlite_vec import serialize_float32

    return serialize_float32(vec)


def _chunks_to_embed(doc) -> list[str]:
    """Return the chunk texts to embed for `doc`, each capped to
    _CHUNK_EMBED_MAX_CHARS.

    Falls back to fixed-size windows over doc.content when the document has
    no chunk metadata (e.g. an extraction path that doesn't populate
    doc.meta['chunks']), so passage-level retrieval still works.
    """
    raw_chunks = doc.meta.get("chunks", []) if doc.meta else []
    texts = []
    for chunk in raw_chunks:
        t = (chunk.get("text") or "").strip()
        if t:
            texts.append(t[:_CHUNK_EMBED_MAX_CHARS])

    if not texts:
        content = doc.content or ""
        texts = [
            content[i : i + _CHUNK_EMBED_MAX_CHARS]
            for i in range(0, len(content), _CHUNK_EMBED_MAX_CHARS)
        ]

    return texts


async def _embed_document_chunks(doc: Document, db, cfg) -> int:
    """Embed and store every chunk for `doc`. Returns the number of chunks written.

    Idempotent: clears any existing chunk rows (and their vec0 rows) for
    this document first, so re-embedding on retry/re-ingestion never hits a
    UNIQUE conflict or leaves orphaned rows. Nothing is committed until
    every chunk succeeds — a mid-loop failure leaves no partial write.
    """
    from sqlalchemy import text as sa_text

    from app.models.database import DocumentChunk

    texts = _chunks_to_embed(doc)
    if not texts:
        return 0

    existing_ids = [
        row[0]
        for row in db.execute(
            sa_text("SELECT id FROM document_chunks WHERE document_id = :doc_id"),
            {"doc_id": doc.id},
        ).fetchall()
    ]
    if existing_ids:
        placeholders = ",".join(str(int(i)) for i in existing_ids)
        db.execute(
            sa_text(
                f"DELETE FROM document_chunk_vectors WHERE chunk_id IN ({placeholders})"
            )
        )
        db.execute(
            sa_text("DELETE FROM document_chunks WHERE document_id = :doc_id"),
            {"doc_id": doc.id},
        )

    written = 0
    with model_gate("embed", label=f"embed:doc:{doc.id}"):
        for idx, chunk_text in enumerate(texts):
            params = await embed_provider.get_embedding_params(
                cfg.embed_model, chunk_text
            )
            async with track_ai_call_async(f"embed:doc:{doc.id}:chunk:{idx}"):
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        params["url"], json=params["json"], headers=params["headers"]
                    )
                    response.raise_for_status()
                    data = response.json()

            embedding = None
            if "embedding" in data:
                embedding = data["embedding"]
            elif (
                "data" in data
                and isinstance(data["data"], list)
                and len(data["data"]) > 0
            ):
                embedding = data["data"][0].get("embedding")

            if not embedding:
                raise ValueError(
                    f"Embedding provider returned no vector for doc {doc.id} chunk {idx}"
                )
            if len(embedding) != cfg.embed_dim:
                raise ValueError(
                    f"Embedding dim mismatch for doc {doc.id} chunk {idx}: provider "
                    f"returned {len(embedding)}, config embed_dim={cfg.embed_dim}. "
                    "Check AI_EMBED_DIM matches the embedding model."
                )

            chunk_row = DocumentChunk(
                document_id=doc.id, chunk_index=idx, text=chunk_text
            )
            db.add(chunk_row)
            db.flush()  # populate chunk_row.id for the vec0 insert below

            blob = _serialize(embedding)
            db.execute(
                sa_text(
                    "INSERT INTO document_chunk_vectors(chunk_id, embedding) "
                    "VALUES (:chunk_id, :embedding)"
                ),
                {"chunk_id": chunk_row.id, "embedding": blob},
            )
            written += 1

    db.commit()
    return written


async def generate_embedding(doc_id: int):
    """Generate and store chunk-level embeddings for a document.

    Raises on any failure (network error, JSON parse, dim mismatch, no vector) —
    the caller (`generate_embedding_task`) catches and marks the stage failed.
    Silent failure here was previously masking docs that never got an embedding
    written but were still flagged COMPLETED, so search would miss them.
    """
    db = SessionLocal()
    # Only recorded once a provider call is actually attempted — a doc with
    # no content (early return below) never called a model, so nothing to log.
    attempted = False
    run_started = time.perf_counter()
    run_status = "error"
    run_error: str | None = None
    resp_len = 0
    cfg = None
    doc = None
    ptype = None
    try:
        embed_provider.reload_from_db(db)
        cfg = get_embed_config(db)
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc or not doc.content or doc.content.startswith("Conversion failed:"):
            return

        attempted = True
        ptype = await embed_provider.get_type()
        written = await _embed_document_chunks(doc, db, cfg)
        resp_len = written
        run_status = "ok"
    except Exception as exc:
        run_error = str(exc)
        raise
    finally:
        if attempted and cfg is not None:
            record_run(
                kind="doc",
                scope_id=str(doc_id),
                stage="embed",
                doc_id=doc_id,
                batch_id=doc.ingest_batch_id if doc else None,
                case_id=doc.case_id if doc else None,
                model=cfg.embed_model,
                provider=ptype,
                duration_ms=int((time.perf_counter() - run_started) * 1000),
                response_len=resp_len,
                status=run_status,
                error=run_error[:200] if run_error else None,
            )
        db.close()


def nearest_chunks(query_text: str, db, *, k: int) -> list[dict]:
    """Return up to `k` chunk hits ranked by vector similarity, closest first.

    Each hit is {chunk_id, document_id, chunk_index, text, distance}.
    Synchronous, best-effort: returns ``[]`` on any failure (provider down,
    dim mismatch, sqlite-vec unavailable) so callers can fall back gracefully.
    """
    from sqlalchemy import text as sa_text

    from app.core.async_utils import run_async

    if not query_text:
        return []
    try:
        embed_provider.reload_from_db(db)
        cfg = get_embed_config(db)
        params = run_async(
            embed_provider.get_embedding_params(cfg.embed_model, query_text)
        )
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                params["url"], json=params["json"], headers=params["headers"]
            )
            resp.raise_for_status()
            data = resp.json()
        embedding = data.get("embedding") or (
            data.get("data", [{}])[0].get("embedding") if data.get("data") else None
        )
        if not embedding or len(embedding) != cfg.embed_dim:
            return []
        blob = _serialize(embedding)
        rows = db.execute(
            sa_text(
                "SELECT chunk_id, distance FROM document_chunk_vectors "
                "WHERE embedding MATCH :blob ORDER BY distance LIMIT :k"
            ),
            {"blob": blob, "k": k},
        ).fetchall()
        chunk_ids = [row[0] for row in rows]
        if not chunk_ids:
            return []
        distances = {row[0]: row[1] for row in rows}

        from app.models.database import DocumentChunk

        chunk_rows = (
            db.query(DocumentChunk).filter(DocumentChunk.id.in_(chunk_ids)).all()
        )
        by_id = {c.id: c for c in chunk_rows}
        return [
            {
                "chunk_id": cid,
                "document_id": by_id[cid].document_id,
                "chunk_index": by_id[cid].chunk_index,
                "text": by_id[cid].text,
                "distance": distances[cid],
            }
            for cid in chunk_ids
            if cid in by_id
        ]
    except Exception as e:  # noqa: BLE001 — best-effort; never block the caller
        logger.debug("nearest_chunks unavailable: %s", e)
        return []


def nearest_document_ids(query_text: str, db, *, k: int) -> list[int]:
    """Return up to `k` document_ids ranked by their best-matching chunk,
    closest first (deduped — a document may own several high-ranked chunks).

    Synchronous, best-effort: returns ``[]`` on any failure. Mirrors the
    embed+vec0 MATCH pattern in ``SearchService._semantic_document_ids``;
    lives here so the document-vector KNN has one home.
    """
    hits = nearest_chunks(query_text, db, k=k * _CHUNK_RETRIEVAL_OVERSAMPLE)
    seen: set[int] = set()
    ordered: list[int] = []
    for hit in hits:
        doc_id = hit["document_id"]
        if doc_id in seen:
            continue
        seen.add(doc_id)
        ordered.append(doc_id)
        if len(ordered) >= k:
            break
    return ordered


_REINDEX_BATCH_SIZE = 50


async def reindex_all_docs(db, progress_cb=None) -> dict:
    """Regenerate chunk embeddings for all documents. Returns {total, reindexed, failed}.

    Paginated in batches of _REINDEX_BATCH_SIZE so a corpus of N thousand
    documents doesn't all sit in Python memory at once. Each doc still
    commits independently (vec0 requires DELETE+INSERT for idempotency).
    Progress is logged at INFO every batch so the user can tail the log.

    progress_cb(reindexed: int, failed: int) is called at each batch
    boundary; the Celery wrapper uses this to update UserSettings so the
    HTMX polling UI advances. Best-effort: callback exceptions are
    swallowed so an SQLite write contention doesn't kill the reindex.
    """
    # The user typically triggers reindex right after changing the embedding
    # model in settings — reload the provider config from DB so we use the
    # new model, not whatever was bound at app boot.
    embed_provider.reload_from_db(db)

    cfg = get_embed_config(db)

    base_query = db.query(Document).filter(Document.content.isnot(None))
    total = base_query.count()
    reindexed = 0
    failed = 0

    offset = 0
    while offset < total:
        batch = (
            base_query.order_by(Document.id)
            .limit(_REINDEX_BATCH_SIZE)
            .offset(offset)
            .all()
        )
        if not batch:
            break
        for doc in batch:
            try:
                if not doc.content or doc.content.startswith("Conversion failed:"):
                    continue
                written = await _embed_document_chunks(doc, db, cfg)
                if written:
                    reindexed += 1
                else:
                    failed += 1
            except Exception as e:
                logger.warning(f"Reindex failed for doc {doc.id}: {e}")
                failed += 1
        offset += _REINDEX_BATCH_SIZE
        logger.info(
            f"reindex_all_docs: {min(offset, total)}/{total} processed "
            f"({reindexed} ok, {failed} failed)"
        )
        if progress_cb is not None:
            try:
                progress_cb(reindexed=reindexed, failed=failed)
            except Exception as cb_err:
                logger.debug(f"reindex progress_cb failed (continuing): {cb_err}")

    return {"total": total, "reindexed": reindexed, "failed": failed}


_VEC0_DIM_RE = __import__("re").compile(
    r"embedding\s+float\s*\[\s*(\d+)\s*\]", __import__("re").IGNORECASE
)


def verify_vec0_dim(db, expected_dim: int) -> tuple[bool, int | None]:
    """Read the document_chunk_vectors vec0 schema and compare its declared dimension.

    Returns (matches, actual_dim). actual_dim is None if the schema can't be parsed.
    Used by the lifespan startup hook to fail loudly if AI_EMBED_DIM was changed
    without recreating the vec0 table — vec0 can't be ALTERed, so a mismatch
    silently breaks every embedding write at the per-write dim guard.
    """
    from sqlalchemy import text

    row = db.execute(
        text("SELECT sql FROM sqlite_master WHERE name = 'document_chunk_vectors'")
    ).fetchone()
    if not row or not row[0]:
        return False, None
    match = _VEC0_DIM_RE.search(row[0])
    if not match:
        return False, None
    actual = int(match.group(1))
    return actual == expected_dim, actual

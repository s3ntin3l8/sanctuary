"""3c — Prepare slicing candidates for a multi-page scanned PDF batch."""

import asyncio
import logging
import os
import re
import time
from typing import Any, NamedTuple

import httpx
import pypdfium2 as pdfium
from PIL import Image
from sqlalchemy.orm import Session

from app.core.async_utils import run_async
from app.core.paths import resolve_storage_path
from app.models.database import IngestBatch
from app.models.enums import IngestBatchStatus
from app.services.ai_config import get_chat_config
from app.services.ai_provider import chat_provider
from app.services.ai_run_index import record_run
from app.services.ingestion import ocr_crosscheck
from app.services.intelligence.prompts import SLICING_CUT_SYSTEM
from app.services.intelligence.schemas import CutJudgment

logger = logging.getLogger(__name__)

_THUMBNAIL_LONG_EDGE = 400
_THUMBNAIL_DPI = 120
_TEXT_HEAD_CHARS = 500
# A page with less OCR text than this is proposed for discarding (separator/blank).
_BLANK_PAGE_CHARS = 20
_TEXT_TAIL_CHARS = 500

# Every page boundary goes to the AI. Above this page count only boundaries with
# at least one heuristic signal do, so a huge scan cannot stall prep.
_AI_MAX_PAGES = int(os.getenv("SLICE_AI_MAX_PAGES", "200"))
# Judgment calls in flight at once; a local model serializes more than this anyway.
_AI_CONCURRENCY = int(os.getenv("SLICE_AI_CONCURRENCY", "4"))
_AI_TIMEOUT_SECONDS = 60.0
_AI_MAX_TOKENS = 300
_PREV_HEAD_CHARS = 300
# The marker must sit at the top of the page: "Anlagen: 3" in a letter's subject
# block is not an enclosure.
_MARKER_ZONE_CHARS = 120
# A page opening with one of these is an attachment, whatever the AI says.
_ATTACHMENT_SIGNALS = frozenset({"enclosure_marker", "transmittal_page"})


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------


def _ocr_page_text(image: Image.Image) -> str:
    """Run lightweight OCR on a single page image; return raw text."""
    try:
        return ocr_crosscheck.read_text(image)
    except Exception as exc:
        logger.warning("OCR failed for page: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Heuristic signals
# ---------------------------------------------------------------------------

_RE_PAGE_NUM = re.compile(r"\b(?:Seite\s+)?(\d+)\s*(?:/|von)\s*(\d+)\b", re.IGNORECASE)
_RE_AZ = re.compile(r"\b\d+\s*[A-Za-z]+\s*\d+/\d{2,4}\b")
_RE_ENCLOSURE = re.compile(r"\b(?:Anlage|Annex|Anhang)\s*[A-Z0-9]*\b", re.IGNORECASE)
_RE_TRANSMITTAL = re.compile(r"\b[ÜU]bertragungsnachweis\b", re.IGNORECASE)
_RE_SALUTATION = re.compile(r"\b(?:Sehr geehrte|Dear|Hiermit|Betreff)\b", re.IGNORECASE)
_RE_SIGNATURE = re.compile(
    r"\b(?:Mit freundlichen Grüßen|Hochachtungsvoll|Yours sincerely)\b", re.IGNORECASE
)
_RE_DATE_LINE = re.compile(
    r"(?:(?:den |vom )?(\d{1,2}\.\d{1,2}\.\d{2,4})|"  # "den 15.04.2026" or "15.04.2026"
    r"(?:Berlin|Hamburg|München|Frankfurt|Köln|Stuttgart|Düsseldorf)[\s,]*(?:den |vom )?(\d{1,2}\.\d{1,2}\.\d{2,4})|"  # City + date
    r"(\d{1,2}\.\d{1,2}\.\d{4})"  # Standalone date
    r")",
    re.IGNORECASE,
)


def _signal_page_reset(prev_tail: str, curr_head: str) -> bool:
    m_prev = _RE_PAGE_NUM.search(prev_tail)
    m_curr = _RE_PAGE_NUM.search(curr_head)
    return bool(m_prev and m_curr and int(m_curr.group(1)) <= 1)


def _signal_az_change(prev_head: str, curr_head: str) -> bool:
    az_prev = set(_RE_AZ.findall(prev_head))
    az_curr = set(_RE_AZ.findall(curr_head))
    return bool(az_prev and az_curr and not az_prev.intersection(az_curr))


def _signal_date_line(prev_tail: str, curr_head: str) -> bool:
    """Check if date line changed between pages (indicates new document)."""
    dates_prev = _RE_DATE_LINE.findall(prev_tail)
    dates_curr = _RE_DATE_LINE.findall(curr_head)
    if not (dates_prev and dates_curr):
        return False
    prev_dates = {d.group() if hasattr(d, "group") else str(d) for d in dates_prev if d}
    curr_dates = {d.group() if hasattr(d, "group") else str(d) for d in dates_curr if d}
    return bool(prev_dates and curr_dates and prev_dates != curr_dates)


def _boundary_signals(prev_head: str, prev_tail: str, curr_head: str) -> list[str]:
    """Names of the heuristics that fire on the boundary before ``curr_head``.

    Hints for the AI judgment, not a gate: a boundary with no signal is still
    judged (enclosure-to-enclosure cuts in court scans carry none).
    """
    signals = []
    if _signal_page_reset(prev_tail, curr_head):
        signals.append("page_reset")
    has_sig = bool(_RE_SIGNATURE.search(prev_tail))
    has_sal = bool(_RE_SALUTATION.search(curr_head))
    if has_sig and has_sal:
        signals.append("salutation_signature")
    elif has_sig:
        signals.append("signature_on_previous_page")
    elif has_sal:
        signals.append("salutation_on_page")
    if len(curr_head.strip()) < _BLANK_PAGE_CHARS:
        signals.append("blank_page")
    if _signal_az_change(prev_head, curr_head):
        signals.append("az_change")
    if _RE_ENCLOSURE.search(curr_head[:_MARKER_ZONE_CHARS]):
        signals.append("enclosure_marker")
    if _RE_TRANSMITTAL.search(curr_head):
        signals.append("transmittal_page")
    if _signal_date_line(prev_tail, curr_head):
        signals.append("date_line_change")
    return signals


# ---------------------------------------------------------------------------
# AI cut judgment
# ---------------------------------------------------------------------------


class _Boundary(NamedTuple):
    """The page boundary before ``page``, with the text the AI judges it on."""

    page: int
    prev_tail: str
    curr_head: str
    prev_head: str = ""
    signals: tuple[str, ...] = ()


def _judgment_prompt(b: _Boundary) -> str:
    signals = ", ".join(b.signals) or "none"
    return (
        f"Page {b.page - 1} starts:\n{b.prev_head[:_PREV_HEAD_CHARS]}\n\n"
        f"Page {b.page - 1} ends:\n{b.prev_tail}\n\n"
        f"Page {b.page} (the page in question) starts:\n{b.curr_head}\n\n"
        f"Heuristic signals: {signals}"
    )


async def _ai_cut_judgment(b: _Boundary, model: str, client: httpx.AsyncClient) -> dict:
    # Does not use call_json_ai: this runs as async tasks via asyncio.gather for
    # parallel boundary detection, uses non-streaming with a tight timeout,
    # and silently falls back to "no cut" on failure — different semantics from
    # the sequential intelligence pipeline helpers.
    try:
        params = await chat_provider.get_generate_params(
            model=model,
            prompt=_judgment_prompt(b),
            system_prompt=SLICING_CUT_SYSTEM,
            stream=False,
            options={
                "num_ctx": 4096,
                "temperature": 0.1,
                "max_tokens": _AI_MAX_TOKENS,
                # The schema grammar keeps a reasoning model from thinking its
                # way past the timeout; it also drops the case-narrative preamble.
                "_response_schema": CutJudgment.model_json_schema(),
                "_schema_name": CutJudgment.__name__,
                "_include_user_context": False,
            },
        )
        ptype = await chat_provider.get_type()
        resp = await client.post(
            params["url"], json=params["json"], headers=params["headers"]
        )
        resp.raise_for_status()
        data = resp.json()

        if ptype == "ollama":
            raw = data.get("response", "")
        else:
            raw = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        from app.services.intelligence._json import parse_json_response

        return parse_json_response(raw)
    except Exception as exc:
        logger.debug("AI cut judgment failed: %s", exc)
        return _conservative_ai_failure(str(exc))


def _conservative_ai_failure(notes: str) -> dict:
    """The single fail-safe shape for a failed AI cut judgment — no cut
    proposed. Used for both a single candidate's failure and, filled per
    candidate, for a whole-batch failure — the two failure modes must not
    disagree on which way to fail."""
    return {"is_new_document": False, "confidence": "low", "notes": notes}


async def _ai_cut_judgments(candidates: list[_Boundary], model: str) -> dict[int, dict]:
    gate = asyncio.Semaphore(_AI_CONCURRENCY)

    async def judge(b: _Boundary, client: httpx.AsyncClient) -> dict:
        async with gate:
            return await _ai_cut_judgment(b, model, client)

    async with httpx.AsyncClient(timeout=httpx.Timeout(_AI_TIMEOUT_SECONDS)) as client:
        results = await asyncio.gather(
            *(judge(b, client) for b in candidates), return_exceptions=True
        )
    out = {}
    for b, result in zip(candidates, results, strict=False):
        if isinstance(result, BaseException):
            out[b.page] = _conservative_ai_failure(str(result))
        else:
            out[b.page] = result
    return out


def _combine_proposed_cuts(
    candidates: list[_Boundary],
    ai_results: dict[int, dict],
    page_count: int,
    marker_pages: frozenset[int] = frozenset(),
) -> list[dict]:
    """Merge judged boundaries with AI judgments into proposed_cuts.

    A candidate with no `ai_results` entry, or an entry without a verdict, gets
    no cut: an unanswered boundary fails the same way as a failed judgment
    (`_conservative_ai_failure`), so an AI outage can never over-cut.

    Each cut also carries a ``kind``: ``letter`` (an independent letter) or
    ``attachment`` (travels with the preceding letter). Anything but an explicit
    ``letter`` from the AI is an attachment, as is every page in ``marker_pages``
    (an Anlage/Annex marker or a transmission-receipt sheet was seen there).
    """
    proposed_cuts = []
    for cand in candidates:
        cut_page = cand.page
        # Validate cut page is in range (hallucination guard for any AI-injected values)
        if not (2 <= cut_page <= page_count):
            continue
        ai = ai_results.get(cut_page, {})
        ai_raw = ai.get("is_new_document", False)
        ai_agrees = (
            ai_raw
            if isinstance(ai_raw, bool)
            else str(ai_raw).strip().lower() in ("true", "1", "yes")
        )
        ai_confidence = ai.get("confidence", "medium")
        if ai_confidence not in ("high", "medium", "low"):
            ai_confidence = "low"
        kind = (
            "letter"
            if str(ai.get("kind", "")).strip().lower() == "letter"
            and cut_page not in marker_pages
            else "attachment"
        )
        if ai_agrees:
            proposed_cuts.append(
                {
                    "page": cut_page,
                    "confidence": ai_confidence,
                    "kind": kind,
                    "notes": ai.get("notes", ""),
                }
            )
    return proposed_cuts


# ---------------------------------------------------------------------------
# Main prepare function
# ---------------------------------------------------------------------------


_PROGRESS_EVERY = 5
_VIEWER_DPI = 150


def render_page_png(pdf_path, page: int, dpi: int = _VIEWER_DPI) -> bytes | None:
    """Render one 1-based page of a PDF as PNG bytes (150 DPI by default); None when out of range."""
    import io

    pdf_doc = pdfium.PdfDocument(str(pdf_path))
    try:
        if not 0 < page <= len(pdf_doc):
            return None
        buf = io.BytesIO()
        pdf_doc[page - 1].render(scale=dpi / 72.0).to_pil().save(buf, format="PNG")
        return buf.getvalue()
    finally:
        pdf_doc.close()


def _write_progress(db: Session, batch: IngestBatch, done: int, total: int, phase: str):
    """Record prep progress for the review page.

    Merges into the existing ``meta["slicing"]`` so ``dispatched_at`` /
    ``recovered`` (read by the stuck-prep sweep) survive.
    """
    meta = dict(batch.meta or {})
    meta["slicing"] = {
        **meta.get("slicing", {}),
        "progress": {"done": done, "total": total, "phase": phase},
    }
    batch.meta = meta
    db.commit()


def prepare(batch_id: int) -> None:
    """Render thumbnails, OCR, judge every page boundary with the AI, write proposed_cuts to batch.meta."""
    from app.config import SessionLocal

    db: Session = SessionLocal()
    try:
        batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
        if not batch:
            logger.warning("prepare_slicing: batch %d not found", batch_id)
            return
        if batch.status != IngestBatchStatus.AWAITING_SLICING:
            logger.info(
                "prepare_slicing: batch %d not in AWAITING_SLICING, skipping", batch_id
            )
            return

        if not batch.raw_source_path:
            # Raise (not return) so the handler below records slicing as failed
            # instead of leaving it "preparing" forever.
            raise ValueError(f"batch {batch_id} has no raw_source_path")
        pdf_path = resolve_storage_path(batch.raw_source_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found at {pdf_path}")

        thumbs_dir = pdf_path.parent / "thumbs"
        thumbs_dir.mkdir(exist_ok=True)

        pdf_doc = pdfium.PdfDocument(str(pdf_path))
        page_count = len(pdf_doc)

        page_data: list[dict[str, Any]] = []

        try:
            for i in range(page_count):
                page = pdf_doc[i]
                bitmap = page.render(scale=_THUMBNAIL_DPI / 72.0)
                img = bitmap.to_pil()

                # OCR the full render: at thumbnail size the text is garbled
                # ("Sete 2", "freundichen") and the boundary regexes miss.
                text = _ocr_page_text(img)

                # Resize long edge to _THUMBNAIL_LONG_EDGE
                w, h = img.size
                long = max(w, h)
                if long > _THUMBNAIL_LONG_EDGE:
                    scale = _THUMBNAIL_LONG_EDGE / long
                    img = img.resize(
                        (int(w * scale), int(h * scale)), Image.Resampling.LANCZOS
                    )

                thumb_path = thumbs_dir / f"page_{i + 1}.png"
                img.save(str(thumb_path))

                page_data.append(
                    {
                        "text_head": text[:_TEXT_HEAD_CHARS],
                        "text_tail": text[-_TEXT_TAIL_CHARS:],
                        "blank": len(text.strip()) < _BLANK_PAGE_CHARS,
                        "thumbnail_path": str(thumb_path),
                    }
                )
                if (i + 1) % _PROGRESS_EVERY == 0 or i + 1 == page_count:
                    _write_progress(db, batch, i + 1, page_count, "ocr")
        finally:
            pdf_doc.close()

        # Every boundary between page i and i+1 (cut position = i+1) gets judged;
        # the heuristic signals only travel with it as hints.
        candidates = [
            _Boundary(
                page=i + 2,
                prev_tail=page_data[i]["text_tail"],
                curr_head=page_data[i + 1]["text_head"],
                prev_head=page_data[i]["text_head"],
                signals=tuple(
                    _boundary_signals(
                        page_data[i]["text_head"],
                        page_data[i]["text_tail"],
                        page_data[i + 1]["text_head"],
                    )
                ),
            )
            for i in range(page_count - 1)
        ]
        if page_count > _AI_MAX_PAGES:
            signalled = [c for c in candidates if c.signals]
            logger.info(
                "prepare_slicing: batch %d has %d pages (> %d) — judging only the "
                "%d boundaries with a heuristic signal, skipping %d",
                batch_id,
                page_count,
                _AI_MAX_PAGES,
                len(signalled),
                len(candidates) - len(signalled),
            )
            candidates = signalled
        marker_pages = {
            c.page for c in candidates if _ATTACHMENT_SIGNALS.intersection(c.signals)
        }

        # AI pass
        if candidates:
            _write_progress(db, batch, page_count, page_count, "ai")
        chat_provider.reload_from_db(db)
        chat_cfg = get_chat_config(db)
        summary_model = chat_cfg.summary_model
        ai_results: dict[int, dict] = {}
        if candidates:
            slice_started = time.perf_counter()
            slice_error: str | None = None
            try:
                ai_results = run_async(_ai_cut_judgments(candidates, summary_model))
            except Exception as exc:
                logger.warning("AI cut judgment batch failed: %s", exc)
                slice_error = str(exc)
                # No verdicts at all: _combine_proposed_cuts proposes nothing.
                ai_results = {}
            # One aggregate entry per slicing run — the candidates fan out to
            # many small parallel judgment calls (see _ai_cut_judgments), and
            # docs don't exist yet at this point, so per-call/per-doc rows
            # aren't meaningful here.
            record_run(
                kind="batch",
                scope_id=str(batch_id),
                stage="slice",
                batch_id=batch_id,
                model=summary_model,
                provider=chat_cfg.provider,
                duration_ms=int((time.perf_counter() - slice_started) * 1000),
                response_len=len(ai_results),
                status="error" if slice_error else "ok",
                error=slice_error[:200] if slice_error else None,
            )

        # Combine heuristic + AI into proposed_cuts
        proposed_cuts = _combine_proposed_cuts(
            candidates, ai_results, page_count, frozenset(marker_pages)
        )

        meta = dict(batch.meta or {})
        meta["slicing"] = {
            "status": "ready",
            "page_count": page_count,
            "pages": page_data,
            "proposed_cuts": proposed_cuts,
        }
        batch.meta = meta
        db.commit()
        logger.info(
            "prepare_slicing: batch %d ready — %d proposed cuts",
            batch_id,
            len(proposed_cuts),
        )

    except Exception as exc:
        logger.error(
            "prepare_slicing batch %d failed: %s", batch_id, exc, exc_info=True
        )
        try:
            batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
            if batch:
                meta = dict(batch.meta or {})
                meta["slicing"] = {"status": "failed", "error": str(exc)}
                batch.meta = meta
                db.commit()
        except Exception:
            db.rollback()
        raise
    finally:
        db.close()

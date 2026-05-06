"""3c — Prepare slicing candidates for a multi-page scanned PDF batch."""

import asyncio
import logging
import os
import re
import threading
from pathlib import Path

import httpx
import pypdfium2 as pdfium
from PIL import Image
from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.core.async_utils import run_async
from app.models.database import IngestBatch
from app.models.enums import IngestBatchStatus
from app.services.ai_config import get_chat_config
from app.services.ai_provider import chat_provider
from app.services.intelligence.prompts import SLICING_CUT_SYSTEM

logger = logging.getLogger(__name__)

_THUMBNAIL_LONG_EDGE = 400
_THUMBNAIL_DPI = 120
_TEXT_HEAD_CHARS = 500
_TEXT_TAIL_CHARS = 500

_W_PAGE_RESET = float(os.getenv("SLICE_W_PAGE_RESET", "0.30"))
_W_LETTERHEAD = float(os.getenv("SLICE_W_LETTERHEAD", "0.20"))
_W_SALUTATION = float(os.getenv("SLICE_W_SALUTATION", "0.20"))
_W_BLANK = float(os.getenv("SLICE_W_BLANK", "0.15"))
_W_AZ_CHANGE = float(os.getenv("SLICE_W_AZ_CHANGE", "0.25"))
_W_ENCLOSURE = float(os.getenv("SLICE_W_ENCLOSURE", "0.30"))
_W_DATE_LINE = float(os.getenv("SLICE_W_DATE_LINE", "0.25"))

_HEURISTIC_THRESHOLD = float(os.getenv("SLICE_HEURISTIC_THRESHOLD", "0.35"))


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------


_ocr_instance = None
_ocr_lock = threading.Lock()


def _get_ocr():
    global _ocr_instance
    if _ocr_instance is None:
        with _ocr_lock:
            if _ocr_instance is None:
                from rapidocr import RapidOCR

                _ocr_instance = RapidOCR()
    return _ocr_instance


def _ocr_page_text(image: Image.Image) -> str:
    """Run lightweight OCR on a single page image; return raw text."""
    try:
        import numpy as np

        ocr = _get_ocr()
        arr = np.array(image.convert("RGB"))
        result, _ = ocr(arr)
        if not result:
            return ""
        return " ".join(r[1] for r in result if r and len(r) > 1)
    except Exception as exc:
        logger.debug("OCR failed for page: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Heuristic signals
# ---------------------------------------------------------------------------

_RE_PAGE_NUM = re.compile(r"\b(?:Seite\s+)?(\d+)\s*/\s*(\d+)\b")
_RE_AZ = re.compile(r"\b\d+\s*[A-Za-z]+\s*\d+/\d{2,4}\b")
_RE_ENCLOSURE = re.compile(r"\b(?:Anlage|Annex|Anhang)\s*[A-Z0-9]*\b", re.IGNORECASE)
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


def _signal_page_reset(prev_tail: str, curr_head: str) -> float:
    m_prev = _RE_PAGE_NUM.search(prev_tail)
    m_curr = _RE_PAGE_NUM.search(curr_head)
    if m_prev and m_curr and int(m_curr.group(1)) <= 1:
        return 1.0
    return 0.0


def _signal_letterhead_change(prev_img: Image.Image, curr_img: Image.Image) -> float:
    """Grayscale average diff on top 20% of thumbnail."""
    import numpy as np

    h = max(1, prev_img.height // 5)
    prev_arr = np.array(
        prev_img.convert("L").crop((0, 0, prev_img.width, h)), dtype=float
    )
    curr_arr = np.array(
        curr_img.convert("L").crop((0, 0, curr_img.width, h)), dtype=float
    )
    if prev_arr.size == 0 or curr_arr.size == 0:
        return 0.0
    diff = abs(float(prev_arr.mean()) - float(curr_arr.mean())) / 255.0
    return min(diff * 2.5, 1.0)


def _signal_salutation_signature(prev_tail: str, curr_head: str) -> float:
    has_sig = bool(_RE_SIGNATURE.search(prev_tail))
    has_sal = bool(_RE_SALUTATION.search(curr_head))
    if has_sig and has_sal:
        return 1.0
    if has_sig or has_sal:
        return 0.4
    return 0.0


def _signal_blank_page(curr_head: str) -> float:
    return 1.0 if len(curr_head.strip()) < 20 else 0.0


def _signal_az_change(prev_head: str, curr_head: str) -> float:
    az_prev = set(_RE_AZ.findall(prev_head))
    az_curr = set(_RE_AZ.findall(curr_head))
    if az_prev and az_curr and not az_prev.intersection(az_curr):
        return 1.0
    return 0.0


def _signal_enclosure_marker(curr_head: str) -> float:
    return 1.0 if _RE_ENCLOSURE.search(curr_head) else 0.0


def _signal_date_line(prev_tail: str, curr_head: str) -> float:
    """Check if date line changed between pages (indicates new document)."""
    dates_prev = _RE_DATE_LINE.findall(prev_tail)
    dates_curr = _RE_DATE_LINE.findall(curr_head)
    if dates_prev and dates_curr:
        prev_dates = {
            d.group() if hasattr(d, "group") else str(d) for d in dates_prev if d
        }
        curr_dates = {
            d.group() if hasattr(d, "group") else str(d) for d in dates_curr if d
        }
        if prev_dates and curr_dates and prev_dates != curr_dates:
            return 1.0
    return 0.0


def _boundary_heuristic_score(
    prev_tail: str,
    curr_head: str,
    prev_img: Image.Image,
    curr_img: Image.Image,
    prev_head: str,
) -> tuple[float, list[str]]:
    signals = []
    score = 0.0

    s = _signal_page_reset(prev_tail, curr_head)
    if s > 0:
        score += s * _W_PAGE_RESET
        signals.append("page_reset")

    s = _signal_letterhead_change(prev_img, curr_img)
    if s > 0:
        score += s * _W_LETTERHEAD
        signals.append(f"letterhead_diff={s:.2f}")

    s = _signal_salutation_signature(prev_tail, curr_head)
    if s > 0:
        score += s * _W_SALUTATION
        signals.append("salutation_signature")

    s = _signal_blank_page(curr_head)
    if s > 0:
        score += s * _W_BLANK
        signals.append("blank_page")

    s = _signal_az_change(prev_head, curr_head)
    if s > 0:
        score += s * _W_AZ_CHANGE
        signals.append("az_change")

    s = _signal_enclosure_marker(curr_head)
    if s > 0:
        score += s * _W_ENCLOSURE
        signals.append("enclosure_marker")

    s = _signal_date_line(prev_tail, curr_head)
    if s > 0:
        score += s * _W_DATE_LINE
        signals.append("date_line_change")

    return score, signals


# ---------------------------------------------------------------------------
# AI cut judgment
# ---------------------------------------------------------------------------


async def _ai_cut_judgment(
    prev_tail: str, curr_head: str, model: str, client: httpx.AsyncClient
) -> dict:
    # Does not use call_json_ai: this runs as async tasks via asyncio.gather for
    # parallel boundary detection, uses non-streaming with a tight 30s timeout,
    # and silently falls back to "no cut" on failure — different semantics from
    # the sequential intelligence pipeline helpers.
    prompt = (
        f"Previous page last {_TEXT_TAIL_CHARS} chars:\n{prev_tail}\n\n"
        f"Current page first {_TEXT_HEAD_CHARS} chars:\n{curr_head}"
    )
    try:
        params = await chat_provider.get_generate_params(
            model=model,
            prompt=prompt,
            system_prompt=SLICING_CUT_SYSTEM,
            stream=False,
            options={"num_ctx": 2048, "temperature": 0.1},
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
        return {"is_new_document": False, "confidence": "low", "notes": str(exc)}


async def _ai_cut_judgments(
    candidates: list[tuple[int, str, str]], model: str
) -> dict[int, dict]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        tasks = [_ai_cut_judgment(pt, ch, model, client) for _, pt, ch in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    out = {}
    for (page_num, _, _), result in zip(candidates, results, strict=False):
        if isinstance(result, Exception):
            out[page_num] = {
                "is_new_document": False,
                "confidence": "low",
                "notes": str(result),
            }
        else:
            out[page_num] = result
    return out


# ---------------------------------------------------------------------------
# Main prepare function
# ---------------------------------------------------------------------------


def prepare(batch_id: int) -> None:
    """Render thumbnails, OCR, run heuristics + AI, write proposed_cuts to batch.meta."""
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

        pdf_path = Path(batch.raw_source_path)
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found at {pdf_path}")

        thumbs_dir = pdf_path.parent / "thumbs"
        thumbs_dir.mkdir(exist_ok=True)

        pdf_doc = pdfium.PdfDocument(str(pdf_path))
        page_count = len(pdf_doc)

        page_data = []
        images: list[Image.Image] = []

        try:
            for i in range(page_count):
                page = pdf_doc[i]
                bitmap = page.render(scale=_THUMBNAIL_DPI / 72.0)
                img = bitmap.to_pil()

                # Resize long edge to _THUMBNAIL_LONG_EDGE
                w, h = img.size
                long = max(w, h)
                if long > _THUMBNAIL_LONG_EDGE:
                    scale = _THUMBNAIL_LONG_EDGE / long
                    img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

                thumb_path = thumbs_dir / f"page_{i + 1}.png"
                img.save(str(thumb_path))
                images.append(img)

                text = _ocr_page_text(img)
                page_data.append(
                    {
                        "text_head": text[:_TEXT_HEAD_CHARS],
                        "text_tail": text[-_TEXT_TAIL_CHARS:],
                        "thumbnail_path": str(thumb_path),
                    }
                )
        finally:
            pdf_doc.close()

        # Heuristic pass: find candidate cuts (between pages i and i+1; cut position = i+1)
        heuristic_candidates = []
        for i in range(page_count - 1):
            score, signals = _boundary_heuristic_score(
                prev_tail=page_data[i]["text_tail"],
                curr_head=page_data[i + 1]["text_head"],
                prev_img=images[i],
                curr_img=images[i + 1],
                prev_head=page_data[i]["text_head"],
            )
            if score >= _HEURISTIC_THRESHOLD:
                heuristic_candidates.append(
                    (i + 2, page_data[i]["text_tail"], page_data[i + 1]["text_head"])
                )

        # AI pass
        chat_provider.reload_from_db(db)
        summary_model = get_chat_config(db).summary_model
        ai_results: dict[int, dict] = {}
        if heuristic_candidates:
            try:
                ai_results = run_async(
                    _ai_cut_judgments(heuristic_candidates, summary_model)
                )
            except Exception as exc:
                logger.warning("AI cut judgment batch failed: %s", exc)

        # Combine heuristic + AI into proposed_cuts
        proposed_cuts = []
        for cut_page, prev_tail, curr_head in heuristic_candidates:
            # Validate cut page is in range (hallucination guard for any AI-injected values)
            if not (2 <= cut_page <= page_count):
                continue
            ai = ai_results.get(cut_page, {})
            ai_raw = ai.get("is_new_document", True)
            ai_agrees = (
                ai_raw
                if isinstance(ai_raw, bool)
                else str(ai_raw).strip().lower() in ("true", "1", "yes")
            )
            ai_confidence = ai.get("confidence", "medium")
            if ai_agrees:
                proposed_cuts.append(
                    {
                        "page": cut_page,
                        "confidence": ai_confidence,
                        "notes": ai.get("notes", ""),
                    }
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

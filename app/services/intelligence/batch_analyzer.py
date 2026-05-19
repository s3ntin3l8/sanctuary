"""4a — Per-batch AI pass: cover-letter detection, originator attribution, action items.

Supports multi-bundle format (new):
- bundles: [{"cover_letter_doc_id": int|null, "enclosed": [...]}]
- Each bundle wires its enclosures to its cover letter

Legacy format (backward compat):
- cover_letter_doc_id, is_cover_letter, enclosed_descriptions
"""

import logging
import re
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.models.database import Document, DocumentRelationship, IngestBatch
from app.models.enums import (
    DocumentRole,
    OriginatorType,
    RelationshipConfidence,
    RelationshipType,
    parse_originator_type,
)
from app.services.ai_config import get_chat_config
from app.services.ai_summary import get_content_preview
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence._party_context import format_party_context
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import (
    BATCH_ANALYZER_SYSTEM,
    fence,
    sanitize_oneline,
)
from app.services.intelligence.schemas import BatchAnalysis

logger = logging.getLogger(__name__)


def _has_manual_groups(batch_id: int, db) -> bool:
    """Return True when the user has manually organized this batch into sub-groups.

    When True, batch_analyzer skips all Document.role assignments to preserve
    the user's manually-set cover letter and enclosure roles.
    """
    from app.models.database import BatchSubGroup

    return (
        db.query(BatchSubGroup)
        .filter(BatchSubGroup.batch_id == batch_id)
        .limit(1)
        .count()
        > 0
    )


COVER_LETTER_KEYWORDS = {
    "begleitschreiben",
    "anschreiben",
    "übersendungsschreiben",
    "deckblatt",
    "cover",
}


def _pick_cover_letter_candidate(docs: list[Document]) -> Document | None:
    """Heuristic: pick the most likely cover letter candidate from the batch.
    Only considers documents with actual content.
    """
    healthy_docs = [d for d in docs if d.content and len(d.content.strip()) > 10]

    for doc in healthy_docs:
        lower = (doc.title or "").lower()
        if any(kw in lower for kw in COVER_LETTER_KEYWORDS):
            return doc

    if healthy_docs:
        # Prefer short docs as cover candidates — always provide one so the AI
        # pipeline runs. The AI decides whether the batch has a real relay letter;
        # _apply_batch_results handles the case where it returns no cover_letter_doc_id.
        return min(healthy_docs, key=lambda d: len(d.content or ""))

    return None


_BUDGET_CHARS = 150_000  # total chars across all doc previews
_MIN_PREVIEW = 3_000
_MAX_PREVIEW = 20_000


def _call_batch_analyzer_sync(
    docs: list[Document],
    batch_id: int,
    model: str = "",
    db=None,
    suppress_thinking: bool = False,
    debug_label: str | None = None,
    party_context: str = "",
) -> dict:
    """Synchronous AI call for batch analysis.

    All documents receive an equal share of the token budget — no single
    'candidate' is given priority. This lets the model identify all cover
    letters from content rather than from a heuristic hint.
    """
    n = len(docs)
    per_doc = min(_MAX_PREVIEW, max(_MIN_PREVIEW, _BUDGET_CHARS // n))

    sections = []
    temporal_map = []
    for d in docs:
        preview = get_content_preview(d, per_doc)
        safe_title = sanitize_oneline(d.title, max_len=120)
        sections.append(
            f"=== (doc_id={d.id}) {safe_title} ===\n{fence(preview, 'batch_doc')}"
        )
        if d.issued_date:
            temporal_map.append(f"doc_{d.id}: {d.issued_date.strftime('%Y-%m-%d')}")

    temporal_block = (
        ("### Batch Temporal Map (Known Dates):\n" + ", ".join(temporal_map) + "\n\n")
        if temporal_map
        else ""
    )

    party_block = (party_context + "\n\n") if party_context else ""
    prompt = party_block + temporal_block + "\n\n".join(sections)

    result = call_json_ai(
        system_prompt=BATCH_ANALYZER_SYSTEM,
        user_prompt=prompt,
        options=STAGE_OPTIONS["batch_analysis"],
        debug_label=debug_label or f"batch_{batch_id}_analyzer",
        schema=BatchAnalysis,
        model=model or None,
        db=db,
        ingest_batch_id=batch_id,
        case_id=docs[0].case_id,
        suppress_thinking=suppress_thinking,
        two_pass=True,
    )
    return result.model_dump()


def _norm_filename(s: str) -> str:
    """Normalize filename for matching."""
    s = re.sub(r"\.[a-zA-Z]{2,5}$", "", s)
    return re.sub(r"[-_.\s]+", " ", s).lower().strip()


def _load_existing_encloses_edges(
    db: Session, doc_ids: list[int]
) -> set[tuple[int, int]]:
    """Return ``{(from_id, to_id), ...}`` for all existing ENCLOSES edges among
    the given doc set. Used to dedupe edge writes in `_apply_batch_results`
    without a per-enclosure SELECT."""
    if not doc_ids:
        return set()
    rows = (
        db.query(
            DocumentRelationship.from_document_id,
            DocumentRelationship.to_document_id,
        )
        .filter(
            DocumentRelationship.from_document_id.in_(doc_ids),
            DocumentRelationship.to_document_id.in_(doc_ids),
            DocumentRelationship.relationship_type == RelationshipType.ENCLOSES,
        )
        .all()
    )
    return {(r[0], r[1]) for r in rows}


def _add_encloses_edge(
    db: Session,
    existing: set[tuple[int, int]],
    cover_id: int,
    enclosure_id: int,
) -> None:
    """Idempotently write a cover→enclosure ENCLOSES edge, tracking writes in
    the supplied ``existing`` set so callers can batch-dedupe across a loop."""
    key = (cover_id, enclosure_id)
    if key in existing:
        return
    db.add(
        DocumentRelationship(
            from_document_id=cover_id,
            to_document_id=enclosure_id,
            relationship_type=RelationshipType.ENCLOSES,
            confidence=RelationshipConfidence.AI_DETECTED,
            notes="batch analyzer: cover→enclosure",
            ingest_date=datetime.now(UTC),
        )
    )
    existing.add(key)


def _apply_batch_results(
    batch_id: int,
    docs: list[Document],
    result: dict,
    db: Session,
    skip_role_assignment: bool = False,
) -> None:
    """Write batch analyzer results to the DB.

    Supports both multi-bundle format (new) and legacy format (backward compat).
    New format: bundles = [{"cover_letter_doc_id": int|null, "enclosed": [...]}]
    Legacy format: cover_letter_doc_id, is_cover_letter, enclosed_descriptions
    """
    if skip_role_assignment:
        return

    bundles = result.get("bundles")
    detected_actions = result.get("detected_actions") or []

    doc_map = {d.id: d for d in docs}
    claimed_ids: set[int] = set()
    cover_ids: set[int] = set()
    first_cover: Document | None = None

    # Pre-load existing ENCLOSES edges among this batch's docs once so the
    # enclosure-wiring loops below don't fire one SELECT per enclosure.
    existing_edges = _load_existing_encloses_edges(db, [d.id for d in docs])

    # Check if we have the new multi-bundle format
    if bundles and isinstance(bundles, list):
        # First pass: declare cover letters so the enclosure pass below can
        # block any attempt to wire a cover as the child of another bundle.
        for bundle in bundles:
            cover_id = bundle.get("cover_letter_doc_id")
            if cover_id is None:
                continue
            if cover_id not in doc_map:
                logger.warning(
                    "Batch #%d: AI returned cover_letter_doc_id=%s not in batch — skipping bundle.",
                    batch_id,
                    cover_id,
                )
                continue
            cover_ids.add(cover_id)

        # Second pass: apply each bundle.
        for bundle in bundles:
            cover_id = bundle.get("cover_letter_doc_id")
            enclosed = bundle.get("enclosed") or []

            cover_doc = doc_map.get(cover_id) if cover_id in cover_ids else None
            if cover_doc:
                cover_doc.role = DocumentRole.COVER_LETTER
                # A cover letter cannot also be an enclosure — clear any stale
                # parent_id from a prior run or earlier bundle.
                cover_doc.parent_id = None
                # court_relay is owned by METADATA (sender=court ∧ originator≠court).
                # Don't overwrite from enclosure types — direct court rulings with
                # court enclosures are not relays.

                # Fill attribution from first enclosure only when the cover letter
                # does not already have its own attributed_originator (e.g. set by
                # Phase-1 metadata from the letterhead). An unconditional overwrite
                # would replace the cover's true sender with the enclosed party name.
                if not cover_doc.attributed_originator:
                    first_enclosure = next(
                        (
                            e.get("attributed_originator")
                            for e in enclosed
                            if e.get("attributed_originator")
                        ),
                        None,
                    )
                    cover_doc.attributed_originator = first_enclosure
                if first_cover is None:
                    first_cover = cover_doc

            # Without a cover letter the AI is signaling the doc is standalone,
            # not enclosed under anything. Skip enclosure wiring and let the
            # unclaimed-fallback at the end mark the doc STANDALONE.
            if cover_id is None or cover_doc is None:
                continue

            # Wire enclosures to this cover letter
            for encl in enclosed:
                matched = encl.get("matched_filename")
                child = None
                if matched:
                    matched_norm = _norm_filename(matched)
                    candidates = [
                        d
                        for d in docs
                        if d.id != cover_id
                        and d.id not in claimed_ids
                        and d.id not in cover_ids
                    ]
                    child = next(
                        (
                            d
                            for d in candidates
                            if _norm_filename(d.title or "") == matched_norm
                        ),
                        None,
                    )
                    if not child:
                        subs = [
                            d
                            for d in candidates
                            if matched_norm in _norm_filename(d.title or "")
                            or _norm_filename(d.title or "") in matched_norm
                        ]
                        if len(subs) == 1:
                            child = subs[0]
                if child:
                    claimed_ids.add(child.id)
                    child.role = DocumentRole.ENCLOSURE
                    child.parent_id = cover_id
                    _add_encloses_edge(db, existing_edges, cover_id, child.id)
                    # Only fill originator_type when metadata had no opinion;
                    # full-text metadata beats batch's title-only context.
                    if child.originator_type in (None, OriginatorType.UNKNOWN):
                        child.originator_type = (
                            parse_originator_type(encl.get("originator_type"))
                            or child.originator_type
                        )
                    if not child.attributed_originator:
                        child.attributed_originator = encl.get("attributed_originator")
    else:
        # Legacy format: single cover letter
        cover_letter_doc_id = result.get("cover_letter_doc_id")
        is_cover_letter = result.get("is_cover_letter", False)
        enclosed_descriptions = result.get("enclosed_descriptions") or []

        if cover_letter_doc_id is not None and cover_letter_doc_id not in doc_map:
            logger.warning(
                "Batch #%d: AI returned cover_letter_doc_id=%s not in batch — skipping bundle.",
                batch_id,
                cover_letter_doc_id,
            )
            cover_letter_doc_id = None

        cover_letter_doc = (
            doc_map.get(cover_letter_doc_id) if cover_letter_doc_id else None
        )
        if cover_letter_doc and is_cover_letter:
            cover_letter_doc.role = DocumentRole.COVER_LETTER
            cover_letter_doc.parent_id = None
            # Fill attribution only when not already set by Phase-1 metadata.
            if not cover_letter_doc.attributed_originator:
                cover_letter_doc.attributed_originator = next(
                    (
                        d.get("attributed_originator")
                        for d in enclosed_descriptions
                        if d.get("attributed_originator")
                    ),
                    None,
                )
            cover_ids.add(cover_letter_doc.id)
            first_cover = cover_letter_doc

        for encl in enclosed_descriptions:
            matched = encl.get("matched_filename")
            child = None
            if matched:
                matched_norm = _norm_filename(matched)
                candidates = [
                    d
                    for d in docs
                    if d.id != cover_letter_doc_id
                    and d.id not in claimed_ids
                    and d.id not in cover_ids
                ]
                child = next(
                    (
                        d
                        for d in candidates
                        if _norm_filename(d.title or "") == matched_norm
                    ),
                    None,
                )
                if not child:
                    subs = [
                        d
                        for d in candidates
                        if matched_norm in _norm_filename(d.title or "")
                        or _norm_filename(d.title or "") in matched_norm
                    ]
                    if len(subs) == 1:
                        child = subs[0]
            if child:
                claimed_ids.add(child.id)
                child.role = DocumentRole.ENCLOSURE
                child.parent_id = cover_letter_doc_id
                _add_encloses_edge(db, existing_edges, cover_letter_doc_id, child.id)
                if child.originator_type in (None, OriginatorType.UNKNOWN):
                    child.originator_type = (
                        parse_originator_type(encl.get("originator_type"))
                        or child.originator_type
                    )
                if not child.attributed_originator:
                    child.attributed_originator = encl.get("attributed_originator")

    # Cascade case/proceeding from any cover letter to all docs
    if first_cover and first_cover.case_id:
        for d in docs:
            if d.id not in claimed_ids and (not d.case_id or d.case_id == "_TRIAGE"):
                d.case_id = first_cover.case_id
                if first_cover.proceeding_id and not d.proceeding_id:
                    d.proceeding_id = first_cover.proceeding_id

    # analyze() already loaded the IngestBatch on this session, so db.get
    # returns it from the identity map without a re-query.
    batch = db.get(IngestBatch, batch_id)
    if (
        batch
        and first_cover
        and first_cover.case_id
        and (not batch.case_id or batch.case_id == "_TRIAGE")
    ):
        batch.case_id = first_cover.case_id
        if first_cover.proceeding_id and not batch.proceeding_id:
            batch.proceeding_id = first_cover.proceeding_id

    # Store batch-level detected actions on the batch so each document enricher
    # can read them as hints and decide which apply to its own document.
    # The enricher is the sole creator of ActionItem rows.
    if batch and detected_actions:
        batch.detected_actions = detected_actions

    # Single-relay fallback: when the AI didn't produce a bundle but exactly
    # one doc in the batch is flagged as a court relay (set in Phase 1 from
    # the letterhead), wire the unclaimed siblings as enclosures of that
    # relay. This is the common "court letter + attachments" shape that
    # doesn't read as a Begleitschreiben to the model.
    if not claimed_ids:
        relays = [d for d in docs if d.court_relay]
        if len(relays) == 1 and len(docs) > 1:
            relay = relays[0]
            relay.role = DocumentRole.COVER_LETTER
            for d in docs:
                if (
                    d.id == relay.id
                    or d.parent_id is not None
                    or d.originator_type
                    in (
                        OriginatorType.OWN,
                        OriginatorType.OPPOSING,
                        OriginatorType.THIRD_PARTY,
                    )
                ):
                    continue
                d.role = DocumentRole.ENCLOSURE
                d.parent_id = relay.id
                claimed_ids.add(d.id)

    # Proceeding-grouping fallback: AI returned no bundles AND single-relay
    # didn't apply. Pick the cover-letter candidate the same way analyze()
    # does and wire siblings sharing its proceeding_id as enclosures. This is
    # the common direct-court-letter-with-attachments shape (sender=court,
    # originator=court — not a relay).
    if not claimed_ids and len(docs) > 1:
        candidate = _pick_cover_letter_candidate(docs)
        if candidate is not None and candidate.proceeding_id:
            siblings_in_proceeding = [
                d
                for d in docs
                if d.id != candidate.id
                and d.proceeding_id == candidate.proceeding_id
                and d.parent_id is None
                # Don't claim other cover letters as enclosures (would create cycles).
                and d.role != DocumentRole.COVER_LETTER
            ]
            if siblings_in_proceeding:
                candidate.role = DocumentRole.COVER_LETTER
                candidate.parent_id = None
                if not candidate.attributed_originator and candidate.sender:
                    candidate.attributed_originator = candidate.sender
                for d in siblings_in_proceeding:
                    if d.originator_type in (
                        OriginatorType.OWN,
                        OriginatorType.OPPOSING,
                        OriginatorType.THIRD_PARTY,
                    ):
                        continue
                    d.role = DocumentRole.ENCLOSURE
                    d.parent_id = candidate.id
                    claimed_ids.add(d.id)
                logger.info(
                    "Batch #%d: AI bundles empty — applied proceeding-grouping "
                    "fallback (cover=%d, %d enclosure(s) sharing proceeding_id=%s).",
                    batch_id,
                    candidate.id,
                    len(claimed_ids),
                    candidate.proceeding_id,
                )

    # Completion sweep: even when the AI (or an earlier fallback) produced
    # bundles, it can under-claim — e.g. the cover letter says "nebst Anlage"
    # singular but the same email contained additional rulings from the same
    # proceeding. For every doc already promoted to COVER_LETTER, claim any
    # unclaimed sibling that shares the cover's proceeding_id. Originator
    # guard keeps own/opposing letters out.
    covers = [d for d in docs if d.role == DocumentRole.COVER_LETTER]
    # Snapshot the cover-letter set so an earlier cover's sweep can't claim a
    # later cover as its enclosure (which would downgrade the second cover and
    # create a cycle once the second cover's own sweep runs).
    cover_letter_ids = {c.id for c in covers}
    for cover in covers:
        if not cover.proceeding_id:
            continue
        swept = 0
        for d in docs:
            if (
                d.id == cover.id
                or d.id in cover_letter_ids
                or d.id in claimed_ids
                or d.parent_id is not None
                or d.proceeding_id != cover.proceeding_id
                or d.originator_type
                in (
                    OriginatorType.OWN,
                    OriginatorType.OPPOSING,
                    OriginatorType.THIRD_PARTY,
                )
            ):
                continue
            d.role = DocumentRole.ENCLOSURE
            d.parent_id = cover.id
            claimed_ids.add(d.id)
            swept += 1
        if swept:
            logger.info(
                "Batch #%d cover doc=%d sweep: claimed %d additional sibling(s) "
                "sharing proceeding_id=%s.",
                batch_id,
                cover.id,
                swept,
                cover.proceeding_id,
            )

    # Downgrade cover letters with no enclosures to STANDALONE.
    # Happens when the AI assigned a cover role but matched no enclosures,
    # or a bundle-assignment error left a cover unclaimed after all wiring.
    child_parent_ids = {d.parent_id for d in docs if d.parent_id is not None}
    for cover in covers:
        if cover.id not in child_parent_ids:
            cover.role = DocumentRole.STANDALONE
            cover.parent_id = None
            logger.info(
                "Batch #%d cover doc=%d has no enclosures — downgrading to STANDALONE.",
                batch_id,
                cover.id,
            )

    # Mark unclaimed docs as STANDALONE
    for d in docs:
        if (
            d.id not in claimed_ids
            and not d.parent_id
            and d.role
            not in (
                DocumentRole.COVER_LETTER,
                DocumentRole.ENCLOSURE,
            )
        ):
            d.role = DocumentRole.STANDALONE

    db.commit()


def analyze(batch_id: int) -> bool:
    """Run the batch-level AI pass for the given IngestBatch.

    Returns True when the AI call ran, False when analysis was skipped
    (single doc or no healthy content). Raises on AI failure so the
    Celery task can retry and update the pipeline stage correctly.

    Three-phase design — see document_enricher.enrich() for the rationale.
    Early-exit writes (standalone assignment) happen in the read session
    because they are fast and don't involve any AI call.
    """
    # --- Phase 1: read (and handle early-exit writes) ---
    db: Session = SessionLocal()
    try:
        cfg = get_chat_config(db)
        from app.services.ai_provider import chat_provider

        chat_provider.reload_from_db(db)
        batch = db.query(IngestBatch).filter(IngestBatch.id == batch_id).first()
        if not batch:
            logger.warning(f"Batch {batch_id} not found for analysis")
            return False

        docs = db.query(Document).filter(Document.ingest_batch_id == batch_id).all()
        if not docs:
            logger.info(f"Batch {batch_id} has no documents to analyze")
            return False

        healthy_docs = [d for d in docs if d.content and len(d.content.strip()) > 10]

        if not healthy_docs or len(healthy_docs) == 1:
            manual_groups = _has_manual_groups(batch_id, db)
            if not manual_groups:
                for d in docs:
                    d.role = DocumentRole.STANDALONE
            db.commit()
            return False

        model = cfg.summary_model
        from app.services.case_service import get_case_opposing_parties
        from app.services.user_settings_service import get_party_identity

        party_identity = get_party_identity(db)
        batch_case_id = docs[0].case_id if docs else None
        case_opposing = get_case_opposing_parties(batch_case_id, db)
        party_context = format_party_context(
            own_self=party_identity.get("own_self", ""),
            own_parties=party_identity.get("own_parties", []),
            opposing_parties=case_opposing,
        )
        # docs objects detach from the session on close but column-level data
        # (content, title, id, …) stays accessible in memory for the AI call.
    finally:
        db.close()

    # --- Phase 2: AI call (no session held) ---
    try:
        result = _call_batch_analyzer_sync(
            healthy_docs,
            batch_id,
            model=model,
            party_context=party_context,
        )
    except ValueError as first_err:
        # AI returned empty/unparseable. Retry once with /no_think — same
        # pattern as METADATA at ai_summary.py:336-356. If the retry also
        # returns empty, fall through to heuristic fallback in
        # _apply_batch_results.
        logger.info(
            "Batch %d analyzer: empty AI response (%s) — retrying with "
            "thinking suppressed.",
            batch_id,
            first_err,
        )
        try:
            result = _call_batch_analyzer_sync(
                healthy_docs,
                batch_id,
                model=model,
                suppress_thinking=True,
                debug_label=f"batch_{batch_id}_analyzerretry",
                party_context=party_context,
            )
        except ValueError as retry_err:
            logger.warning(
                "Batch %d: analyzer empty after /no_think retry (%s) — "
                "applying heuristic fallback.",
                batch_id,
                retry_err,
            )
            result = {}
    except Exception as e:
        logger.error(f"Batch {batch_id} analysis failed: {e}", exc_info=True)
        db = SessionLocal()
        try:
            has_manual = _has_manual_groups(batch_id, db)
            if not has_manual:
                docs_err = (
                    db.query(Document)
                    .filter(Document.ingest_batch_id == batch_id)
                    .all()
                )
                for d in docs_err:
                    d.role = DocumentRole.STANDALONE
            db.commit()
        finally:
            db.close()
        raise

    # --- Phase 3: write results ---
    db = SessionLocal()
    try:
        docs_write = (
            db.query(Document).filter(Document.ingest_batch_id == batch_id).all()
        )
        has_manual = _has_manual_groups(batch_id, db)
        _apply_batch_results(
            batch_id, docs_write, result, db, skip_role_assignment=has_manual
        )
        logger.info(f"Batch {batch_id} analyzed successfully")
        return True
    finally:
        db.close()

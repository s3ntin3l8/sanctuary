import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.database import Case, Document, Proceeding
from app.models.enums import OriginatorType, ProceedingCourtLevel, ProceedingStatus
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.content_gate import is_content_ai_ready
from app.services.intelligence.prompts import PROCEEDING_ANALYZER_SYSTEM
from app.services.intelligence.schemas import ProceedingExtraction

logger = logging.getLogger(__name__)


def extract_proceeding_details(
    doc: Document, model: str, db: Session | None = None
) -> dict:
    content = doc.content or ""
    user_prompt = f"Document Title: {doc.title}\nContent:\n{content[:8000]}"
    try:
        result = call_json_ai(
            system_prompt=PROCEEDING_ANALYZER_SYSTEM,
            user_prompt=user_prompt,
            options=STAGE_OPTIONS["proceeding"],
            debug_label=f"doc_{doc.id}_proceeding",
            schema=ProceedingExtraction,
            model=model or None,
            db=db,
            ingest_batch_id=doc.ingest_batch_id,
            two_pass=True,
        )
        return result.model_dump()
    except Exception as e:
        logger.error(f"Failed to extract proceeding details for doc {doc.id}: {e}")
        return {}


def analyze_and_update_proceeding(doc: Document, model: str, db: Session) -> str | None:
    """Analyze document, update proceeding, or create a new one. Returns skip reason or None."""
    if not is_content_ai_ready(doc):
        return "content too short"

    if not doc.case_id or doc.case_id == "_TRIAGE":
        return "no case assigned"

    data = extract_proceeding_details(doc, model, db=db)

    from app.services.ingestion.extractors import infer_court_level, normalize_az_court

    # Fallback for AI failure (empty response, parse error, server timeout):
    # if METADATA already extracted an AZ for a court letter, use that instead
    # of skipping silently. Bounded by originator_type=COURT to avoid creating
    # bogus proceedings from polluted az_court hints on lawyer/own letters.
    if (
        not doc.proceeding_id
        and not data.get("is_court_document")
        and doc.az_court
        and doc.originator_type == OriginatorType.COURT
    ):
        inferred_level = infer_court_level(doc.sender)
        data = {
            "is_court_document": True,
            "az_court": doc.az_court,
            "court_name": data.get("court_name") or doc.sender,
            "court_level": data.get("court_level")
            or (inferred_level.value if inferred_level else None),
            "subject_matter": data.get("subject_matter"),
        }
        logger.info(
            "Doc %d: proceeding-analyzer AI empty/uncertain — falling back to "
            "Document.az_court=%s from METADATA.",
            doc.id,
            doc.az_court,
        )

    if not data.get("is_court_document"):
        return "not a court document"

    extracted_az = normalize_az_court(data.get("az_court"))
    extracted_level_str = data.get("court_level")

    try:
        extracted_level = (
            ProceedingCourtLevel(extracted_level_str.lower())
            if extracted_level_str
            else None
        )
    except ValueError:
        extracted_level = None

    # No proceeding linked yet: find-or-create one for (case_id, az_court).
    if not doc.proceeding_id:
        existing = None
        if extracted_az:
            existing = (
                db.query(Proceeding)
                .filter(
                    Proceeding.case_id == doc.case_id,
                    Proceeding.az_court == extracted_az,
                )
                .first()
            )
        if existing:
            doc.proceeding_id = existing.id
        else:
            # Before creating a new proceeding, check for a placeholder created by
            # METADATA when the court was not yet known (court_name="General", no AZ).
            placeholder = None
            if extracted_az:
                placeholder = (
                    db.query(Proceeding)
                    .filter(
                        Proceeding.case_id == doc.case_id,
                        Proceeding.court_name.in_(["General", "Unknown Court"]),
                        Proceeding.az_court.is_(None),
                    )
                    .first()
                )
            if placeholder:
                placeholder.az_court = extracted_az
                if extracted_level:
                    placeholder.court_level = extracted_level
                if data.get("court_name"):
                    placeholder.court_name = data["court_name"]
                if data.get("subject_matter") and not placeholder.subject_matter:
                    placeholder.subject_matter = data["subject_matter"]
                doc.proceeding_id = placeholder.id
            elif extracted_az:
                # AI-detected proceeding — inherit draft state from parent case.
                parent_case = db.query(Case).filter(Case.id == doc.case_id).first()
                new_proc = Proceeding(
                    case_id=doc.case_id,
                    court_name=data.get("court_name") or "Unknown Court",
                    court_level=extracted_level or ProceedingCourtLevel.AG,
                    az_court=extracted_az,
                    subject_matter=data.get("subject_matter"),
                    status=ProceedingStatus.ACTIVE,
                    started_at=datetime.now(UTC),
                    is_draft=bool(parent_case and parent_case.is_draft),
                )
                db.add(new_proc)
                db.flush()
                doc.proceeding_id = new_proc.id
            else:
                db.commit()
                return "no az extracted"

        # Cascade to batch only when batch has no proceeding yet — siblings
        # with different AZs are handled by their own runs.
        if doc.ingest_batch and not doc.ingest_batch.proceeding_id:
            doc.ingest_batch.proceeding_id = doc.proceeding_id

        db.commit()
        return None

    current_proc = (
        db.query(Proceeding).filter(Proceeding.id == doc.proceeding_id).first()
    )
    if not current_proc:
        return "proceeding not found"

    # Logic: Should we escalate/create a new proceeding?
    is_new_instance = False

    # Hierarchy check
    levels = list(ProceedingCourtLevel)
    # Filter out OTHER if it's there, but ProceedingCourtLevel usually has AG, LG, OLG, BGH
    # AG is index 0, LG 1, OLG 2, BGH 3

    if extracted_level and current_proc.court_level:
        try:
            if levels.index(extracted_level) > levels.index(current_proc.court_level):
                is_new_instance = True
        except ValueError:
            pass

    # AZ check
    if extracted_az and current_proc.az_court and extracted_az != current_proc.az_court:
        is_new_instance = True

    if is_new_instance:
        # Before creating, check if a proceeding with this AZ already exists in the case.
        existing_match = None
        if extracted_az:
            existing_match = (
                db.query(Proceeding)
                .filter(
                    Proceeding.case_id == current_proc.case_id,
                    Proceeding.az_court == extracted_az,
                    Proceeding.id != current_proc.id,
                )
                .first()
            )

        if existing_match:
            # Reuse the existing proceeding — avoids duplicates from whitespace-variant AZ extractions.
            new_proc = existing_match
            if current_proc.az_court != extracted_az:
                current_proc.status = ProceedingStatus.CLOSED
                current_proc.ended_at = datetime.now(UTC)
        else:
            # AI-detected escalation — inherit draft state from parent case.
            parent_case = db.query(Case).filter(Case.id == current_proc.case_id).first()
            new_proc = Proceeding(
                case_id=current_proc.case_id,
                court_name=data.get("court_name") or "Unknown Court",
                court_level=extracted_level or ProceedingCourtLevel.AG,
                az_court=extracted_az,
                subject_matter=data.get("subject_matter"),
                status=ProceedingStatus.ACTIVE,
                started_at=datetime.now(UTC),
                is_draft=bool(parent_case and parent_case.is_draft),
            )
            db.add(new_proc)

            # Close old
            current_proc.status = ProceedingStatus.CLOSED
            current_proc.ended_at = datetime.now(UTC)

            db.flush()  # Get new_proc.id

        # Update doc
        doc.proceeding_id = new_proc.id
        if doc.ingest_batch:
            doc.ingest_batch.proceeding_id = new_proc.id
            for batch_doc in doc.ingest_batch.documents:
                batch_doc.proceeding_id = new_proc.id
    else:
        # Auto-fill current
        if not current_proc.az_court and extracted_az:
            current_proc.az_court = extracted_az
        if (
            not current_proc.court_name
            or current_proc.court_name in ("Unknown Court", "General")
        ) and data.get("court_name"):
            current_proc.court_name = data.get("court_name")
        if not current_proc.subject_matter and data.get("subject_matter"):
            current_proc.subject_matter = data.get("subject_matter")
        # Upgrade placeholder levels (AG default or OTHER unknown) when we now know more
        if extracted_level and current_proc.court_level in (
            ProceedingCourtLevel.AG,
            ProceedingCourtLevel.OTHER,
        ):
            current_proc.court_level = extracted_level

    db.commit()
    return None

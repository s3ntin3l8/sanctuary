import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import Document, Proceeding
from app.models.enums import ProceedingCourtLevel, ProceedingStatus
from app.services.ai_provider import call_llm

logger = logging.getLogger(__name__)

PROCEEDING_PROMPT = """
You are a German legal AI assistant. Analyze this document and extract proceeding details.
Extract:
1. "is_court_document": boolean
2. "court_level": string (strictly one of: AG, LG, OLG, BGH) or null
3. "court_name": string (e.g. "Amtsgericht Hamburg") or null
4. "az_court": string (the court file number, e.g. "003 F 426/25") or null
5. "subject_matter": string or null
6. "appeal_deadline_days": integer (if this is a ruling with a formal deadline, extract the days, else null)

Respond ONLY with a valid JSON object.

Document Title: {title}
Content:
{content}
"""


def extract_proceeding_details(doc: Document, model: str) -> dict:
    content = doc.content or ""
    prompt = PROCEEDING_PROMPT.format(title=doc.title, content=content[:8000])
    try:
        response = call_llm(
            prompt,
            model=model,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        # Clean response in case there's markdown wrap
        clean_response = response.strip()
        if clean_response.startswith("```json"):
            clean_response = clean_response[7:]
        if clean_response.endswith("```"):
            clean_response = clean_response[:-3]

        return json.loads(clean_response)
    except Exception as e:
        logger.error(f"Failed to extract proceeding details for doc {doc.id}: {e}")
        return {}


def analyze_and_update_proceeding(doc: Document, model: str, db: Session) -> str | None:
    """Analyze document, update proceeding, or create a new one. Returns skip reason or None."""
    if not doc.content or len(doc.content) < 50:
        return "content too short"

    if not doc.proceeding_id:
        return "no proceeding assigned"

    current_proc = (
        db.query(Proceeding).filter(Proceeding.id == doc.proceeding_id).first()
    )
    if not current_proc:
        return "proceeding not found"

    data = extract_proceeding_details(doc, model)
    if not data.get("is_court_document"):
        return "not a court document"

    from app.services.ingestion.extractors import normalize_az_court

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
                current_proc.ended_at = datetime.now()
        else:
            new_proc = Proceeding(
                case_id=current_proc.case_id,
                court_name=data.get("court_name") or "Unknown Court",
                court_level=extracted_level or ProceedingCourtLevel.AG,
                az_court=extracted_az,
                subject_matter=data.get("subject_matter"),
                status=ProceedingStatus.ACTIVE,
                started_at=datetime.now(),
            )
            db.add(new_proc)

            # Close old
            current_proc.status = ProceedingStatus.CLOSED
            current_proc.ended_at = datetime.now()

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
            not current_proc.court_name or current_proc.court_name == "Unknown Court"
        ) and data.get("court_name"):
            current_proc.court_name = data.get("court_name")
        if not current_proc.subject_matter and data.get("subject_matter"):
            current_proc.subject_matter = data.get("subject_matter")
        # If it was AG but now we know more (and it's not a new instance)
        if extracted_level and current_proc.court_level == ProceedingCourtLevel.AG:
            current_proc.court_level = extracted_level

    db.commit()
    return None

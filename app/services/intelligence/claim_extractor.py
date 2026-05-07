"""4c — Per-document claim extraction: new Claim rows + ClaimEvidence stances on existing claims."""

import logging

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.models.database import Claim, Document
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    SignificanceTier,
)
from app.repositories.claim import ClaimRepository
from app.repositories.claim_evidence import ClaimEvidenceRepository
from app.services.ai_config import get_chat_config
from app.services.ai_summary import get_content_preview
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import CLAIM_EXTRACTOR_SYSTEM
from app.services.intelligence.schemas import ClaimExtraction

logger = logging.getLogger(__name__)

ELIGIBLE_TIERS = {SignificanceTier.CRITICAL, SignificanceTier.SIGNIFICANT}
VALID_CLAIM_TYPES = {e.value for e in ClaimType}
VALID_EVIDENCE_ROLES = {e.value for e in ClaimEvidenceRole}
MAX_EXISTING_CLAIMS = 20


def _format_existing_claims(claims: list[Claim]) -> str:
    if not claims:
        return "(none)"
    lines = []
    for c in claims:
        lines.append(
            f"ID={c.id} | type={c.claim_type.value} | status={c.status.value} | {c.claim_text[:200]}"
        )
    return "\n".join(lines)


def _call_claim_extractor_sync(
    doc: Document, existing_claims: list[Claim], model: str = "", db=None
) -> dict:
    content_preview = get_content_preview(doc, 60000)
    mgmt = doc.ai_summary or {}
    legal_sig = mgmt.get("legal_significance", "")

    existing_text = _format_existing_claims(existing_claims)
    prompt = (
        f"DOCUMENT TITLE: {doc.title}\n"
        f"LEGAL SUMMARY: {legal_sig}\n\n"
        f"CONTENT:\n{content_preview}\n\n"
        f"EXISTING OPEN CLAIMS IN THIS CASE:\n{existing_text}"
    )

    result = call_json_ai(
        system_prompt=CLAIM_EXTRACTOR_SYSTEM,
        user_prompt=prompt,
        options=STAGE_OPTIONS["claims"],
        debug_label=f"doc_{doc.id}_claims",
        schema=ClaimExtraction,
        model=model or None,
        db=db,
        ingest_batch_id=doc.ingest_batch_id,
        two_pass=True,
    )
    return result.model_dump()


def _apply_claims(
    doc: Document, result: dict, existing_claims: list[Claim], db: Session
) -> None:
    claim_repo = ClaimRepository(db)
    evidence_repo = ClaimEvidenceRepository(db)
    valid_claim_ids = {c.id for c in existing_claims}

    # Build a set of normalized existing claim texts for dedupe
    seen_texts: set[str] = {
        " ".join(c.claim_text.lower().split())[:80] for c in existing_claims
    }

    # Create new claims
    for item in result.get("new_claims") or []:
        claim_text = (item.get("claim_text") or "").strip()
        if not claim_text:
            continue
        if len(claim_text) < 30:
            logger.info(
                f"Doc {doc.id}: claim too short ({len(claim_text)} chars), dropping"
            )
            continue
        normalized = " ".join(claim_text.lower().split())[:80]
        if normalized in seen_texts:
            logger.info(f"Doc {doc.id}: duplicate claim text, dropping")
            continue
        seen_texts.add(normalized)
        claim_type_raw = (item.get("claim_type") or "").lower()
        if claim_type_raw not in VALID_CLAIM_TYPES:
            logger.info(
                f"Doc {doc.id}: invalid claim_type '{claim_type_raw}', dropping"
            )
            continue

        claim = claim_repo.create_claim(
            case_id=doc.case_id,
            proceeding_id=doc.proceeding_id,
            source_document_id=doc.id,
            claim_text=claim_text,
            claim_type=ClaimType(claim_type_raw),
        )
        # Source doc supports its own new claim
        evidence_repo.link(
            claim_id=claim.id,
            document_id=doc.id,
            role=ClaimEvidenceRole.SUPPORTS,
            excerpt=(item.get("excerpt") or "")[:500],
        )

    # Link evidence to existing claims
    for item in result.get("evidence_links") or []:
        claim_id = item.get("claim_id")
        if claim_id not in valid_claim_ids:
            logger.info(
                f"Doc {doc.id}: evidence_link claim_id {claim_id} not in candidates, dropping"
            )
            continue

        role_raw = (item.get("role") or "").lower()
        if role_raw not in VALID_EVIDENCE_ROLES:
            logger.info(f"Doc {doc.id}: invalid evidence role '{role_raw}', dropping")
            continue

        role = ClaimEvidenceRole(role_raw)
        if evidence_repo.evidence_exists(claim_id, doc.id, role):
            continue

        evidence_repo.link(
            claim_id=claim_id,
            document_id=doc.id,
            role=role,
            excerpt=(item.get("excerpt") or "")[:500],
        )

        # Status transitions
        if role == ClaimEvidenceRole.CONTESTS:
            target = claim_repo.get(claim_id)
            if target and target.status == ClaimStatus.ASSERTED:
                claim_repo.update_status(claim_id, ClaimStatus.CONTESTED)
        elif role == ClaimEvidenceRole.REFUTES:
            claim_repo.update_status(claim_id, ClaimStatus.REFUTED)


def extract(doc_id: int) -> str | None:
    """Extract claims from a single document.

    Returns a non-empty skip reason if skipped, or None if it ran.
    """
    db: Session = SessionLocal()
    try:
        cfg = get_chat_config(db)
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Doc {doc_id} not found for claim extraction")
            return

        if doc.significance_tier not in ELIGIBLE_TIERS:
            reason = f"ineligible_tier:{doc.significance_tier}"
            logger.info(f"Doc {doc_id}: {reason}, skipping claim extraction")
            return reason

        if not doc.content or doc.content.startswith("Conversion failed:"):
            reason = "no_content"
            logger.info(f"Doc {doc_id}: {reason}, skipping claim extraction")
            return reason

        if not doc.case_id or doc.case_id == "_TRIAGE":
            reason = "triage_pending"
            logger.info(f"Doc {doc_id}: {reason}, skipping claim extraction")
            return reason

        claim_repo = ClaimRepository(db)
        existing_claims = list(
            claim_repo.get_open_in_case(doc.case_id, limit=MAX_EXISTING_CLAIMS)
        )

        try:
            result = _call_claim_extractor_sync(
                doc, existing_claims, model=cfg.summary_model, db=db
            )
            _apply_claims(doc, result, existing_claims, db)
            db.commit()
            new_count = len(result.get("new_claims") or [])
            link_count = len(result.get("evidence_links") or [])
            logger.info(
                f"Doc {doc_id}: claim extraction done — {new_count} new, {link_count} evidence links"
            )
        except Exception:
            db.rollback()
            raise
    finally:
        db.close()

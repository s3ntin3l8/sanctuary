"""4c — Per-document claim extraction: new Claim rows + ClaimEvidence stances on existing claims."""

import json
import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.config import DATA_DIR, SessionLocal
from app.core.async_utils import run_async
from app.models.database import Claim, Document
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    SignificanceTier,
)
from app.repositories.claim import ClaimRepository
from app.repositories.claim_evidence import ClaimEvidenceRepository
from app.services.ai_config import get_effective_config
from app.services.ai_provider import ai_provider
from app.services.ai_summary import get_content_preview
from app.services.intelligence._json import parse_json_response
from app.services.intelligence.prompts import CLAIM_EXTRACTOR_SYSTEM

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
    doc: Document, existing_claims: list[Claim], debug_file: str, model: str = ""
) -> dict:
    content_preview = get_content_preview(doc, 6000)
    mgmt = doc.ai_summary or {}
    legal_sig = mgmt.get("legal_significance", "")

    existing_text = _format_existing_claims(existing_claims)
    prompt = (
        f"DOCUMENT TITLE: {doc.title}\n"
        f"LEGAL SUMMARY: {legal_sig}\n\n"
        f"CONTENT:\n{content_preview}\n\n"
        f"EXISTING OPEN CLAIMS IN THIS CASE:\n{existing_text}"
    )

    params = run_async(
        ai_provider.get_generate_params(
            model=model or get_effective_config().summary_model,
            prompt=prompt,
            system_prompt=CLAIM_EXTRACTOR_SYSTEM,
            stream=True,
            options={
                "num_ctx": 8192,
                "temperature": 0.1,
                "num_predict": 1500,
                "max_tokens": 1500,
            },
        )
    )
    ptype = run_async(ai_provider.get_type())

    full_response = ""
    with httpx.Client(timeout=httpx.Timeout(120.0, read=60.0)) as client:
        with open(debug_file, "a") as f:
            f.write(f"--- CLAIM EXTRACTOR doc_id={doc.id} ---\n")
            f.write(f"Payload: {json.dumps(params['json'])}\n\n")

        with client.stream(
            "POST", params["url"], json=params["json"], headers=params["headers"]
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                chunk = ai_provider.parse_stream_line(line, ptype)
                if not chunk:
                    continue
                if "response" in chunk:
                    full_response += chunk["response"]
                if chunk.get("done"):
                    break

        with open(debug_file, "a") as f:
            f.write(f"\n--- END. Length: {len(full_response)} ---\n")

    if not full_response.strip():
        raise ValueError(f"Claim extractor returned empty response for doc {doc.id}")

    return parse_json_response(full_response)


def _apply_claims(
    doc: Document, result: dict, existing_claims: list[Claim], db: Session
) -> None:
    claim_repo = ClaimRepository(db)
    evidence_repo = ClaimEvidenceRepository(db)
    valid_claim_ids = {c.id for c in existing_claims}

    # Create new claims
    for item in result.get("new_claims") or []:
        claim_text = (item.get("claim_text") or "").strip()
        if not claim_text:
            continue
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


def extract(doc_id: int) -> None:
    """Extract claims from a single document."""
    db: Session = SessionLocal()
    try:
        cfg = get_effective_config(db)
        ai_provider.reload_from_db(db)
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Doc {doc_id} not found for claim extraction")
            return

        if doc.significance_tier not in ELIGIBLE_TIERS:
            logger.info(
                f"Doc {doc_id} tier {doc.significance_tier} ineligible for claim extraction"
            )
            return

        if not doc.content or doc.content.startswith("Conversion failed:"):
            logger.info(
                f"Doc {doc_id} has no usable content, skipping claim extraction"
            )
            return

        if not doc.case_id or doc.case_id == "_TRIAGE":
            logger.info(f"Doc {doc_id} in triage/unassigned, skipping claim extraction")
            return

        claim_repo = ClaimRepository(db)
        existing_claims = list(
            claim_repo.get_open_in_case(doc.case_id, limit=MAX_EXISTING_CLAIMS)
        )

        debug_dir = DATA_DIR / "ai_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_file = str(
            debug_dir / f"doc_{doc_id}_{int(datetime.now().timestamp())}_claims.log"
        )

        try:
            result = _call_claim_extractor_sync(
                doc, existing_claims, debug_file, model=cfg.summary_model
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

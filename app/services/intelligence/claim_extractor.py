"""4c — Per-document claim extraction.

Wave 2B pipeline:
1. Pull top-K embedding-nearest existing claims for the case as candidate
   context for `evidence_links`.
2. Run the extractor LLM → returns `new_claims` + `evidence_links`.
3. For each new_claim: create the global Claim row, write its ASSERTS
   evidence row, embed it, then ask the dedup judge to compare against
   the global top-K nearest. High-confidence matches → ClaimMergeProposal.
4. For each evidence_link: write a ClaimEvidenceProposal (no auto-apply
   of ClaimEvidence rows or status changes — Wave 1's wrong-REFUTES
   problem was a direct consequence of auto-applying these). Confirmed
   proposals from the UI become ClaimEvidence rows downstream.
"""

import asyncio
import logging

from sqlalchemy.orm import Session

from app.config import SessionLocal
from app.core.timezone import naive_utc_now
from app.models.database import (
    Claim,
    ClaimEvidenceProposal,
    Document,
)
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    DocumentType,
    OriginatorType,
    ProposalConfidence,
    ProposalStatus,
    SignificanceTier,
)
from app.repositories.claim import ClaimRepository
from app.repositories.claim_evidence import ClaimEvidenceRepository
from app.services.ai_config import get_chat_config
from app.services.ai_summary import get_content_preview
from app.services.claim_embedding import upsert_claim_embedding
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.claim_dedup_judge import propose_merges_for_new_claim
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
    doc: Document, existing_claims: list[Claim], model: str = ""
) -> dict:
    """AI call only — no DB session held."""
    content_preview = get_content_preview(doc, 60000)
    mgmt = doc.ai_summary or {}
    legal_sig = mgmt.get("legal_significance", "")

    from app.services.intelligence.prompts import fence, sanitize_oneline

    existing_text = _format_existing_claims(existing_claims)
    originator_value = doc.originator_type.value if doc.originator_type else "unknown"
    prompt = (
        f"DOCUMENT TITLE: {sanitize_oneline(doc.title, 200)}\n"
        f"DOCUMENT ORIGINATOR: {originator_value}\n"
        f"LEGAL SUMMARY: {fence(legal_sig, 'ai_extracted')}\n\n"
        f"CONTENT:\n{fence(content_preview, 'document')}\n\n"
        f"EXISTING OPEN CLAIMS IN THIS CASE:\n{existing_text}"
    )

    result = call_json_ai(
        system_prompt=CLAIM_EXTRACTOR_SYSTEM,
        user_prompt=prompt,
        options=STAGE_OPTIONS["claims"],
        debug_label=f"doc_{doc.id}_claims",
        schema=ClaimExtraction,
        model=model or None,
        ingest_batch_id=doc.ingest_batch_id,
        case_id=doc.case_id,
        two_pass=True,
    )
    return result.model_dump()


def _apply_claims(
    doc: Document, result: dict, existing_claims: list[Claim], db: Session
) -> None:
    claim_repo = ClaimRepository(db)
    evidence_repo = ClaimEvidenceRepository(db)
    snapshot_ids = {c.id for c in existing_claims}

    # Re-validate against the current DB state.  Between Phase 1 (snapshot)
    # and Phase 3 (write) the AI call takes several minutes.  Concurrent
    # workers running Phase 1 for *other* documents delete their own stale
    # ASSERTED claims during that window, which can remove IDs that are in
    # our snapshot.  Inserting a ClaimEvidenceProposal whose target_claim_id
    # no longer exists raises a FK constraint.  One cheap SELECT prevents that.
    if snapshot_ids:
        live_ids: set[int] = {
            r[0] for r in db.query(Claim.id).filter(Claim.id.in_(snapshot_ids)).all()
        }
        dropped = snapshot_ids - live_ids
        if dropped:
            logger.info(
                "Doc %d: %d candidate claim(s) deleted between Phase 1 and Phase 3 "
                "(stale-cleanup race) — dropping evidence links for IDs %s",
                doc.id,
                len(dropped),
                sorted(dropped),
            )
    else:
        live_ids = set()
    valid_claim_ids = live_ids

    # Build a set of normalized existing claim texts for dedupe
    seen_texts: set[str] = {
        " ".join(c.claim_text.lower().split())[:80] for c in existing_claims
    }

    # Pass 1: create all claims + evidence rows (each create_claim flushes
    # immediately, which starts a write transaction). Collect IDs so we can
    # reload claims after committing — the commit releases the write lock
    # before any embedding HTTP calls, preventing SQLITE_BUSY contention.
    new_claim_ids: list[int] = []
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

        # Court-originated claims arrive ESTABLISHED — they are the
        # procedural reality. But a court RELAY (sender=court forwarding a
        # party submission) carries the party's claims, not the court's;
        # those stay ASSERTED. When attributed_originator names the true
        # author, it overrides originator_type for this decision —
        # same convention as case_graph_service._lane_for.
        effective_ot = doc.originator_type
        if doc.attributed_originator:
            try:
                effective_ot = OriginatorType(doc.attributed_originator)
            except ValueError:
                pass  # display name, not a role key — fall back
        is_court_origination = (
            effective_ot == OriginatorType.COURT and not doc.court_relay
        )
        initial_status = (
            ClaimStatus.ESTABLISHED if is_court_origination else ClaimStatus.ASSERTED
        )

        claim = claim_repo.create_claim(
            claim_text=claim_text,
            claim_type=ClaimType(claim_type_raw),
            status=initial_status,
        )
        # The source document ASSERTS its own new claim — this is the
        # canonical "originated by" evidence row. Other documents can later
        # SUPPORT, CONTEST, or REFUTE (via evidence proposals).
        evidence_repo.link(
            claim_id=claim.id,
            document_id=doc.id,
            role=ClaimEvidenceRole.ASSERTS,
            excerpt=(item.get("excerpt") or "")[:500],
        )
        new_claim_ids.append(claim.id)

    # Wave 2B: evidence_links no longer auto-apply. Each one becomes a
    # ClaimEvidenceProposal awaiting user confirmation. The user-confirmation
    # gate prevents the wrong-REFUTES bug that emerged when the AI
    # mis-interpreted what "the document" referred to in cross-doc evidence.
    confidence_default = ProposalConfidence.MEDIUM
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

        # Skip duplicates: don't re-propose if the same role already lands
        # on this (claim, doc) pair, either as confirmed evidence or as a
        # pending proposal.
        if evidence_repo.evidence_exists(claim_id, doc.id, role):
            continue
        existing_proposal = (
            db.query(ClaimEvidenceProposal)
            .filter(
                ClaimEvidenceProposal.target_claim_id == claim_id,
                ClaimEvidenceProposal.source_document_id == doc.id,
                ClaimEvidenceProposal.proposed_role == role,
                ClaimEvidenceProposal.status == ProposalStatus.PENDING,
            )
            .first()
        )
        if existing_proposal:
            continue

        db.add(
            ClaimEvidenceProposal(
                target_claim_id=claim_id,
                source_document_id=doc.id,
                proposed_role=role,
                excerpt=(item.get("excerpt") or "")[:500],
                rationale=None,
                confidence=confidence_default,
                status=ProposalStatus.PENDING,
                proposed_at=naive_utc_now(),
            )
        )
    db.flush()

    # Commit claims + evidence + proposals before embedding. Each
    # create_claim calls flush() which starts a write transaction; holding
    # that open during an embedding HTTP call (up to 60 s) blocks every
    # other concurrent writer past busy_timeout. Committing here releases
    # the write lock; embedding and dedup calls each manage their own
    # short-lived write transactions.
    db.commit()

    # Pass 2: embed + dedup for each newly created claim. Reload each claim
    # after the commit (SQLAlchemy expires objects on commit).
    for claim_id in new_claim_ids:
        claim = db.get(Claim, claim_id)
        if not claim:
            continue
        try:
            asyncio.run(upsert_claim_embedding(claim_id, db))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Doc %s: failed to embed new claim %s: %s", doc.id, claim_id, exc
            )
        try:
            propose_merges_for_new_claim(claim, db)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Doc %s: dedup judge failed for claim %s: %s", doc.id, claim_id, exc
            )


def extract(doc_id: int) -> str | None:
    """Extract claims from a single document.

    Returns a non-empty skip reason if skipped, or None if it ran.
    """
    # Phase 1: read + stale-claim cleanup (brief write, commits before AI call)
    db: Session = SessionLocal()
    try:
        cfg = get_chat_config(db)
        from app.services.ai_provider import chat_provider

        chat_provider.reload_from_db(db)
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Doc {doc_id} not found for claim extraction")
            return

        if doc.significance_tier not in ELIGIBLE_TIERS:
            reason = f"ineligible_tier:{doc.significance_tier}"
            logger.info(f"Doc {doc_id}: {reason}, skipping claim extraction")
            return reason

        # Cover letters (Begleitschreiben) carry no substantive claims — only
        # letterhead / case-number metadata. Skip even when batch analysis
        # didn't downgrade their tier yet.
        if doc.document_type == DocumentType.RELAY:
            reason = "document_type:relay"
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

        # Clear stale auto-extracted claims from prior runs so retries don't
        # accumulate. The right discriminator is *cross-doc evidence*, not
        # status: a claim is safe to drop only when this document is its sole
        # source. Status alone is misleading — when this doc's originator
        # changes between runs (e.g. court→opposing), _apply_claims' initial
        # status assignment changes too, so old ESTABLISHED claims rooted
        # only in this doc become orphaned under the new originator and must
        # be cleaned out before re-extraction. Claims with confirmed evidence
        # from ANY other document (SUPPORTS, CONTESTS, REFUTES, CITES_AS_PROOF)
        # carry independent signal and are preserved regardless of status.
        # ClaimEvidence rows for the deleted claims cascade via FK
        # ondelete=CASCADE.
        stale_repo = ClaimRepository(db)
        stale = stale_repo.claims_only_originated_by_document(doc.id)
        if stale:
            logger.info(
                f"Doc {doc_id}: clearing {len(stale)} stale auto-originated "
                f"claim(s) (no cross-doc evidence) before re-extraction"
            )
            for c in stale:
                db.delete(c)
            db.commit()

        claim_repo = ClaimRepository(db)
        existing_claims = list(
            claim_repo.get_open_in_case(doc.case_id, limit=MAX_EXISTING_CLAIMS)
        )
        model = cfg.summary_model
        # doc and existing_claims remain accessible after session closes
    finally:
        db.close()

    # Phase 2: AI call — no DB session held
    result = _call_claim_extractor_sync(doc, existing_claims, model=model)

    # Phase 3: write
    db = SessionLocal()
    try:
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

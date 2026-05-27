"""Shared helpers for "is this document a confirmed court source?" checks.

Used by guards across the intelligence pipeline (batch_analyzer originator
override, ai_summary metadata writeback, etc.) to refuse classifying a
clearly-court document as a party-side document. Centralized here so the
court-name keyword list lives in one place.
"""

import logging

from app.models.database import Document
from app.models.enums import DocumentType

logger = logging.getLogger(__name__)

_COURT_SENDER_FRAGMENTS = (
    "amtsgericht",
    "landgericht",
    "oberlandesgericht",
    "bundesgerichtshof",
    "bundesverfassungsgericht",
    "verwaltungsgericht",
    "sozialgericht",
    "arbeitsgericht",
    "finanzgericht",
)


def is_court_name(name: str | None) -> bool:
    """Return True when a name string contains a recognized German court term.

    Substring match against `_COURT_SENDER_FRAGMENTS`, case-insensitive.
    Useful both for sender fields on Document and for free-text party names
    on case.opposing_parties / case.parties.
    """
    if not name:
        return False
    lower = name.lower()
    return any(fragment in lower for fragment in _COURT_SENDER_FRAGMENTS)


# Substrings that uniquely identify entities that PHASE1_METADATA_SYSTEM's
# `third_party defaults` block already labels as `third_party`. Keeps the
# rule in lockstep with the prompt — every fragment below must correspond to
# a rule in `prompts.py:277-286`.
#
# Used to break the feedback loop where `case_brief_generator._compute_parties`
# aggregates a misclassified `originator_type=opposing` vote into
# `case.parties`, which then propagates back to Phase 1 via the Known Party
# Identity block and self-confirms the wrong role on every re-run.
_THIRD_PARTY_DEFAULT_FRAGMENTS = (
    # State treasuries that collect court fees on behalf of the judiciary.
    # Not the court, not a party — doc 7 (Landesjustizkasse Bamberg) regression.
    "landesjustizkasse",
    "gerichtskasse",
    "justizvollzugskasse",
    # Court-appointed family-law roles.
    "verfahrensbeistand",
    "verfahrenspfleger",
    "jugendamt",
    # Court-appointed experts (Sachverständiger, Gutachter) and the orgs they
    # act through. "sachverständig" covers the noun, adjective, and gendered
    # forms; "sachverstaendig" covers the unfolded ASCII spelling.
    "sachverständig",
    "sachverstaendig",
    "gutachter",
    # Credentialing institutions (added Round 6 to PHASE1_METADATA_SYSTEM).
    "notar",  # matches "Notar", "Notariat" — and harmlessly e.g.
    # "notariell" if it appears in a name (rare).
    "übersetz",  # sworn translator: "Übersetzer", "Übersetzungsbüro"
    "uebersetz",
    "standesamt",
    "grundbuchamt",
    "handelsregister",
    # Banks named in the prompt rule. Avoid the bare word "bank" — too prone
    # to false matches on stray words. Add specific German institutions and
    # the ICBC name we've seen in case 8441-25 (doc 44).
    "icbc",
    "sparkasse",
    "volksbank",
    "raiffeisenbank",
    "deutsche bank",
)


def is_third_party_default_name(name: str | None) -> bool:
    """Return True when a name should be locked to `originator_type=third_party`.

    Parallel to `is_court_name`. Matches institutions that the metadata
    system prompt explicitly enumerates as third_party by default — closing
    the feedback loop that lets a misclassified document override the rule
    via the Known Party Identity block.
    """
    if not name:
        return False
    lower = name.lower()
    return any(fragment in lower for fragment in _THIRD_PARTY_DEFAULT_FRAGMENTS)


def is_confirmed_court_document(doc: Document) -> bool:
    """Return True when static metadata confirms a court is the author.

    Guards downstream AI-driven overrides from downgrading
    Phase-1-COURT documents back to a party type. RULING (Beschluss/Urteil)
    and RELAY (Begleitschreiben) are court-only document types — only courts
    issue rulings or relay correspondence. A sender naming a recognized German
    court institution is also a reliable signal.
    """
    if doc.document_type in (DocumentType.RULING, DocumentType.RELAY):
        return True
    return is_court_name(doc.sender)


# Document types that, by definition, are party-authored (Klage, Antrag,
# Beschwerde, Widerspruch fall under MOTION; Klageerwiderung, Stellungnahme
# fall under STATEMENT). The system prompt already says originator must not
# be `court` for these — the reconciler enforces it.
_PARTY_AUTHORED_DOC_TYPES = frozenset(
    {DocumentType.MOTION.value, DocumentType.STATEMENT.value}
)


def reconcile_ai_fields(doc: Document, ai_output: dict) -> list[str]:
    """Mutate `ai_output` in place to resolve AI self-contradictions.

    Fires only when the AI's own fields contradict each other or violate the
    system prompt's own stated rules. Not a judgment override: each rule
    picks the field most likely to be correct (sender / is_court_document /
    document_type are high-confidence signals; originator and court_relay
    are the ones the AI most often gets wrong) and clears the conflicting
    one to `unknown` / `false` so downstream code can re-classify.

    Returns a list of rule identifiers that fired; the caller is expected
    to log them so the contradiction rate is visible in production logs.
    """
    fired: list[str] = []

    # Rule 1 — AI says "not a court document" but emits originator_type=court.
    # Mutates the AI-output dict so the metadata apply step writes the
    # corrected value (only fires on the metadata stage; enricher does not
    # emit `originator_type` directly through ai_output).
    if (
        ai_output.get("is_court_document") is False
        and ai_output.get("originator_type") == "court"
    ):
        ai_output["originator_type"] = "unknown"
        fired.append("R1_not_court_doc_but_court_originator")

    # Rule 2 — court_relay=true requires the letterhead sender to name a
    # court institution. The AI sometimes emits court_relay=true on lawyer
    # letters that forward court rulings (the reverse of the schema's intent).
    if ai_output.get("court_relay") is True:
        sender = ai_output.get("sender") or (doc.sender or "")
        if not is_court_name(sender):
            ai_output["court_relay"] = False
            fired.append("R2_court_relay_but_non_court_sender")

    # Rule 3 — MOTION (Klage, Antrag, Beschwerde, Widerspruch) and STATEMENT
    # (Klageerwiderung, Stellungnahme) are party-authored by definition.
    # The effective doc_type is the AI's new value (enricher stage) or the
    # current value on the document. The effective originator is the AI's
    # new value (metadata stage) or the current value on the document.
    # Mutate both surfaces:
    #   - ai_output["originator_type"] for the metadata stage's apply layer,
    #   - doc.originator_type directly for the enricher stage (whose apply
    #     layer does not read `originator_type` from ai_output — it's not in
    #     DocumentEnrichment).
    from app.models.enums import OriginatorType  # local import: avoid cycle

    new_doc_type = ai_output.get("document_type")
    effective_doc_type = new_doc_type or (
        doc.document_type.value if doc.document_type else None
    )
    new_originator = ai_output.get("originator_type")
    effective_originator = new_originator or (
        doc.originator_type.value if doc.originator_type else None
    )
    if (
        effective_doc_type in _PARTY_AUTHORED_DOC_TYPES
        and effective_originator == "court"
    ):
        ai_output["originator_type"] = "unknown"
        # Enricher stage doesn't read `originator_type` from ai_output —
        # mutate the document directly so the rule takes effect when fired
        # from there. (new_originator is None means the AI in this call
        # didn't touch originator, which only happens in the enricher stage.)
        if new_originator is None:
            doc.originator_type = OriginatorType.UNKNOWN
        fired.append("R3_party_authored_type_but_court_originator")

    for rule in fired:
        logger.warning(
            "Doc %s: AI self-contradiction reconciled — %s "
            "(sender=%r, originator_type=%r, court_relay=%r, doc_type=%r)",
            doc.id,
            rule,
            ai_output.get("sender") or doc.sender,
            ai_output.get("originator_type"),
            ai_output.get("court_relay"),
            ai_output.get("document_type")
            or (doc.document_type.value if doc.document_type else None),
        )

    return fired

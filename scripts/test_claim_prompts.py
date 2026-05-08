"""Prompt iteration harness for the claim extractor.

Runs multiple system-prompt variants against the same fixed corpus of test
documents, calls the AI directly (bypassing the DB-write path of
`_apply_claims`), and scores each output via an LLM-as-judge pass that
classifies every emitted claim as GOOD / META / DOC_REF / SELF_REF / BORDERLINE.

Run with: PYTHONPATH=. .venv/bin/python scripts/test_claim_prompts.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from app.config import SessionLocal
from app.models.database import Claim, Document
from app.repositories.claim import ClaimRepository
from app.services.ai_config import get_chat_config
from app.services.ai_summary import get_content_preview
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence.ai_options import STAGE_OPTIONS
from app.services.intelligence.prompts import CLAIM_EXTRACTOR_SYSTEM
from app.services.intelligence.schemas import ClaimExtraction

TEST_DOC_IDS = [6, 7, 9]
MAX_EXISTING_CLAIMS = 20


# ---------------------------------------------------------------------------
# Prompt variants
# ---------------------------------------------------------------------------


# V1: the current Wave 1 prompt (baseline).
V1_BASELINE = CLAIM_EXTRACTOR_SYSTEM


# V2: V1 + explicit self-reference ban + sharper procedural-mention examples.
V2_SELF_REF_BAN = """You are a legal document analyst building a Truth Map of factual, legal, and procedural assertions ("grounds") that shape the case.

You will be given:
1. A document (title, originator, legal summary, and content preview)
2. A list of EXISTING OPEN CLAIMS in this case (each with id, claim_type, and claim_text)

The DOCUMENT ORIGINATOR tells you who authored this document:
- `court` — extract substantive holdings (legal principles, factual determinations, procedural rulings) but NOT bookkeeping references to other documents in the chain.
- `opposing` / `own` / `third_party` — extract substantive assertions made by the author about the world or the case.

Your tasks:
A) Extract atomic NEW assertions this document makes for the first time (new_claims).
B) Identify if this document takes a stance on any of the listed existing claims (evidence_links).

Return ONLY valid JSON:
{
  "new_claims": [
    {"claim_text": "one atomic assertion", "claim_type": "factual|legal|procedural", "excerpt": "the exact sentence or passage that makes this assertion"}
  ],
  "evidence_links": [
    {"claim_id": <integer from the provided list>, "role": "supports|contests|refutes|cites_as_proof", "excerpt": "the specific passage that supports this stance"}
  ]
}

# THE CONTESTABILITY TEST

A claim is something a reasonable opponent COULD dispute. Before extracting any candidate, ask:
  "If this is wrong, what changes about the case?"
If the answer is "nothing" — DO NOT extract it.

# WHAT IS A CLAIM (the only things to extract)

- **Substantive factual assertions about the world** — what happened, who did what, when, where. NOT what document recorded it.
- **Legal positions or doctrines invoked** — propositions of law, statute readings, case-law references.
- **Court findings and dispositions on matters in controversy** — holdings on the merits, not mere procedural acts.

# WHAT IS NOT A CLAIM (DO NOT EXTRACT)

## NEVER extract self-referential claims about THIS document
The document you are analyzing is the SOURCE of claims, not their SUBJECT. Never claim:
- "This document is dated X" / "the issue date is X" / "the document is dated X"
- "This document is addressed to X" / "the recipient is X" / "the addressee is X"
- "The sender of this document is X" / "this document was sent by X"
- "The internal reference number of this document is X" / "the AZ is X"
- "This document is filed by X" / "we filed this motion" / "this is a complaint"
- "The author of this document is X" / "this document was written by X"

If the assertion is about the very document being analyzed, IT IS NOT A CLAIM. It is metadata. OMIT IT.

## NEVER extract pure document references
Statements that ANOTHER document exists / was filed / was served / was issued. The other document, when ingested, carries its own claims. Examples to OMIT:
- "Yingying Liu filed an objection on 16.01.2026" → reference, not a claim
- "The lawyer responded on 01.12.2025" → reference, not a claim
- "The lower court's decision was served on 17.01.2026" → reference
- "The AG issued a decision on 12.11.2025" → reference

DISTINGUISHING TEST: if the sentence's load-bearing meaning is **the existence/timing of a document or filing**, it's a reference. If the load-bearing meaning is **a substantive fact about the world or the dispute**, it might be a claim.
- "The lower court rejected the request because § 180 III requirements were not substantiated" → SUBSTANTIVE HOLDING (claim, type=procedural or legal)
- "The lower court issued a decision on 15.01.2026" → REFERENCE (omit)

## NEVER extract directives, deadlines, action items
- "wird dem Gläubiger aufgegeben, … binnen 2 Wochen mitzuteilen" → action_item
- "Der Antragsgegnerin wird … gesetzt" → action_item
- Any "by date X, do Y" → action_item, not a claim

## NEVER extract boilerplate or letterhead
- Letterhead identity (who the Urkundsbeamtin is, court address)
- Signature blocks
- "Datenschutzhinweis…", "elektronisch erstellt und ist ohne Unterschrift gültig"
- "The court has jurisdiction" (boilerplate, not contested in this matter)

## NEVER extract acknowledgements/restatements of known facts
- "confirms receipt on date X" — bookkeeping
- "the hearing is scheduled for Y" — calendar entry
- Restatement of identifiers, dates, parties, addresses

# ATOMICITY

Each new_claim is ONE atomic assertion — one subject, one predicate, one claim. Split compound sentences.
claim_type must be exactly one of: factual, legal, procedural.
role must be exactly one of: supports, contests, refutes, cites_as_proof.
Only use claim_ids from the provided existing claims list — never invent IDs.

# PARTY PERSPECTIVE

When the document refers to a party by role label ("der Gläubiger", "der Antragsteller", "der Kläger", "der Schuldner", "die Antragsgegnerin", "die Beklagte", etc.) AND the document context (Rubrum, letterhead, addressee) plus the user-context preamble at the top of this system prompt make clear which party holds that role, write the explicit party name in claim_text. Do not leave a role label generic when the mapping is determinable.

# CALIBRATION

If in doubt, OMIT. A document with zero claims is BETTER than a document with three trivial ones.
Do not pad. Do not extract content just because the document mentions it.
If no extractable new claims and no stances on existing claims: return {"new_claims": [], "evidence_links": []}.
Do not deliberate or self-correct. Output the JSON immediately.
Return ONLY valid JSON."""


# V3: V2 + few-shot examples (concrete bad/good claims with reasons)
V3_FEW_SHOT = V2_SELF_REF_BAN.replace(
    "# CALIBRATION",
    """# WORKED EXAMPLES

These are based on real extraction failures. Internalize the pattern.

Document: "We, on behalf of Hansen Björn, file this complaint against the LG decision of 24.04.2026 regarding the forced sale order."
- BAD: "Hansen Björn filed a complaint against the LG decision of 24.04.2026" → REJECTED (this IS the document; it's self-referential metadata)
- BAD: "The LG issued a decision on 24.04.2026 regarding the forced sale order" → REJECTED (document reference)
- GOOD: (none) → this content is purely about the document's own filing; no extractable claim.

Document: "The LG ruled that suspension of the partition auction is permissible only for up to six months. The requirements for temporary suspension under § 180 III ZVG were not substantiated."
- GOOD: "Suspension of the partition auction is permissible only for up to six months" → legal doctrine/holding
- GOOD: "The § 180 III ZVG requirements for temporary suspension were not substantiated in this proceeding" → procedural finding
- BAD: "The LG ruled on the case" → too generic, no substantive content

Document letterhead: "Landgericht Ingolstadt, Auf der Schanz 37, 85049 Ingolstadt, AZ 15 T 158/26"
- BAD (all of these): "The court is at Auf der Schanz 37", "The case number is 15 T 158/26", "The document is from LG Ingolstadt" → letterhead metadata, not claims.

Document content: "We dispute that the auction was properly ordered because the children's welfare was not considered."
- GOOD: "The auction was not properly ordered" → contestable factual/procedural assertion (originator-side claim)
- GOOD: "Children's welfare was not adequately considered in the auction order" → contestable factual claim
- BAD: "We dispute the auction" → too generic, no specific proposition

# CALIBRATION""",
)


# ---------------------------------------------------------------------------
# Judge — classifies each emitted claim
# ---------------------------------------------------------------------------


JUDGE_SYSTEM = """You are a strict legal-claim quality classifier. Given a single claim text, classify it as exactly one of:

- GOOD: a contestable, substantive proposition (factual, legal, or procedural finding) about the world or the dispute. If false, the case shifts.
- META: letterhead / metadata about a document (date, address, reference number, sender, recipient, AZ).
- DOC_REF: refers to ANOTHER document by date+actor — "X filed Y on date Z", "decision served on date Q". The load-bearing content is the existence of that other filing.
- SELF_REF: claim is about THE document the author wrote — "this is a complaint", "we filed this motion", "this document is dated X". Self-referential.
- BORDERLINE: arguably substantive but extremely thin or duplicative.

Return ONLY valid JSON:
{"category": "GOOD|META|DOC_REF|SELF_REF|BORDERLINE", "rationale": "one sentence"}
"""


class JudgeOut(BaseModel):
    category: str = Field(pattern=r"^(GOOD|META|DOC_REF|SELF_REF|BORDERLINE)$")
    rationale: str = ""


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class VariantResult:
    variant: str
    doc_id: int
    new_claims: list[dict] = field(default_factory=list)
    evidence_links: list[dict] = field(default_factory=list)
    judgments: list[dict] = field(default_factory=list)
    error: str | None = None


def _format_existing_claims(claims: list[Claim]) -> str:
    if not claims:
        return "(none)"
    return "\n".join(
        f"ID={c.id} | type={c.claim_type.value} | status={c.status.value} | {c.claim_text[:200]}"
        for c in claims
    )


def run_variant(
    variant_name: str, system_prompt: str, doc_id: int, model: str, db
) -> VariantResult:
    res = VariantResult(variant=variant_name, doc_id=doc_id)
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        res.error = f"doc {doc_id} not found"
        return res

    content_preview = get_content_preview(doc, 60000)
    legal_sig = (doc.ai_summary or {}).get("legal_significance", "")
    originator_value = doc.originator_type.value if doc.originator_type else "unknown"

    repo = ClaimRepository(db)
    existing = list(repo.get_open_in_case(doc.case_id, limit=MAX_EXISTING_CLAIMS))

    user_prompt = (
        f"DOCUMENT TITLE: {doc.title}\n"
        f"DOCUMENT ORIGINATOR: {originator_value}\n"
        f"LEGAL SUMMARY: {legal_sig}\n\n"
        f"CONTENT:\n{content_preview}\n\n"
        f"EXISTING OPEN CLAIMS IN THIS CASE:\n{_format_existing_claims(existing)}"
    )

    try:
        result = call_json_ai(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            options=STAGE_OPTIONS["claims"],
            debug_label=f"prompt_test_{variant_name}_{doc_id}",
            schema=ClaimExtraction,
            model=model or None,
            db=db,  # needed so chat_provider routes to the active instance
            two_pass=True,
        )
        data = result.model_dump()
        res.new_claims = data.get("new_claims") or []
        res.evidence_links = data.get("evidence_links") or []
    except Exception as exc:  # noqa: BLE001
        res.error = f"extract failed: {exc}"

    return res


def judge_claim(claim_text: str, doc_title: str, model: str, db) -> dict[str, Any]:
    user_prompt = f'DOCUMENT TITLE: "{doc_title}"\nCLAIM: "{claim_text}"'
    try:
        result = call_json_ai(
            system_prompt=JUDGE_SYSTEM,
            user_prompt=user_prompt,
            options={"temperature": 0.0, "num_predict": 200},
            debug_label="prompt_test_judge",
            schema=JudgeOut,
            model=model or None,
            db=db,
            two_pass=False,
        )
        return result.model_dump()
    except Exception as exc:  # noqa: BLE001
        return {"category": "ERROR", "rationale": str(exc)}


def render_summary(results: list[VariantResult]) -> None:
    by_doc: dict[int, dict[str, VariantResult]] = {}
    for r in results:
        by_doc.setdefault(r.doc_id, {})[r.variant] = r

    for doc_id, variants in by_doc.items():
        print(f"\n{'=' * 80}")
        print(f"DOC {doc_id}")
        print(f"{'=' * 80}")
        for variant_name, r in variants.items():
            cat_counts: dict[str, int] = {}
            for j in r.judgments:
                cat_counts[j["category"]] = cat_counts.get(j["category"], 0) + 1
            print(f"\n  [{variant_name}]  {len(r.new_claims)} claims  {cat_counts}")
            if r.error:
                print(f"    ERROR: {r.error}")
                continue
            for nc, j in zip(r.new_claims, r.judgments, strict=False):
                marker = {
                    "GOOD": "✓",
                    "META": "⚠META",
                    "DOC_REF": "⚠DOC_REF",
                    "SELF_REF": "⚠SELF_REF",
                    "BORDERLINE": "?",
                    "ERROR": "!",
                }.get(j["category"], "?")
                print(
                    f"    {marker:<10}  {nc.get('claim_text', '')[:90]}  →  {j['rationale'][:60]}"
                )


def main() -> int:
    variants = {
        "v1_baseline": V1_BASELINE,
        "v2_self_ref_ban": V2_SELF_REF_BAN,
        "v3_few_shot": V3_FEW_SHOT,
    }

    db = SessionLocal()
    try:
        cfg = get_chat_config(db)
        model = cfg.summary_model
        print(f"Using model: {model}")

        results: list[VariantResult] = []
        for variant_name, system_prompt in variants.items():
            for doc_id in TEST_DOC_IDS:
                print(f"  running {variant_name} on doc {doc_id}...")
                r = run_variant(variant_name, system_prompt, doc_id, model, db)
                results.append(r)
    finally:
        db.close()

    # Judge each emitted claim
    db = SessionLocal()
    try:
        cfg = get_chat_config(db)
        model = cfg.summary_model
        for r in results:
            doc = db.query(Document).filter(Document.id == r.doc_id).first()
            doc_title = doc.title if doc else f"doc {r.doc_id}"
            for nc in r.new_claims:
                j = judge_claim(nc.get("claim_text", ""), doc_title, model, db)
                r.judgments.append(j)
    finally:
        db.close()

    render_summary(results)

    print(f"\n{'=' * 80}")
    print("AGGREGATE BY VARIANT")
    print(f"{'=' * 80}")
    by_variant: dict[str, dict[str, int]] = {}
    for r in results:
        agg = by_variant.setdefault(
            r.variant,
            {
                "total": 0,
                "GOOD": 0,
                "META": 0,
                "DOC_REF": 0,
                "SELF_REF": 0,
                "BORDERLINE": 0,
                "ERROR": 0,
            },
        )
        agg["total"] += len(r.new_claims)
        for j in r.judgments:
            agg[j["category"]] = agg.get(j["category"], 0) + 1

    for variant_name, counts in by_variant.items():
        good = counts["GOOD"]
        total = counts["total"]
        ratio = f"{good}/{total}" if total else "0/0"
        noise = counts["META"] + counts["DOC_REF"] + counts["SELF_REF"]
        print(f"  {variant_name}:  GOOD {ratio}  noise={noise}  rest={counts}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

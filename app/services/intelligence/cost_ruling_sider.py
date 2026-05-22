"""Focused LLM helper: side a §91 ZPO / §81 FamFG cost ruling.

The main document analyzer already extracts the abstract allocation shape
(``{"loser": 1.0}`` / ``{"each_own": true}`` / explicit fractions) when a
document is enriched. What it sometimes misses — especially on legacy data
ingested before the prompt was updated — is which side is the client. The
calculator defaults to "we lost" when that information is absent, which is
the safe pessimistic guess but is wrong whenever the opposing party was the
loser.

This module exposes a single small entrypoint that takes an existing
``CostSignal`` row, replays the ruling text through a tightly scoped
LLM call together with the case's party-identity block, and returns a new
``allocation`` dict with ``client_role`` filled in (and an
``auto_detected: True`` marker so the UI can distinguish it from a manual
flip).
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.models.database import CostSignal, Document
from app.services.ai_config import get_chat_config
from app.services.case_service import get_case_opposing_parties
from app.services.intelligence._ai_call import call_json_ai
from app.services.intelligence._party_context import format_party_context
from app.services.user_settings_service import get_party_identity

logger = logging.getLogger(__name__)


_OPTIONS = {"temperature": 0.0, "num_predict": 200}
# Cap on document text we feed the model. Tenor + rationale of a German
# Kostenentscheidung are nearly always within the first few KB; the rest is
# usually procedural boilerplate and noise.
_MAX_CHARS = 4000


class CostRulingSide(BaseModel):
    """Output schema for the cost-ruling sider call."""

    model_config = ConfigDict(extra="ignore")

    client_role: Literal["winner", "loser", "shared", "each_own", "unknown"]
    rationale: str = ""


_SYSTEM_PROMPT = """You are a German legal-cost analyst.

You receive an excerpt from a German court ruling (Beschluss / Urteil) that
contains a Kostenentscheidung (§91 ZPO, §81 FamFG, or similar). Your job is
to identify who bears the costs, expressed from the client's (Mandant's)
perspective:

- "winner"  — the opposing party bears the costs (the client prevailed)
- "loser"   — the client bears the costs (the client did not prevail)
- "shared"  — costs are apportioned in fractions between the parties
- "each_own"— each party bears its own costs (§81 FamFG default)
- "unknown" — the ruling text doesn't clearly identify a cost-bearer, or the
              party labels can't be resolved against the known identities

Use the Known Party Identity block to translate party role labels
("Antragsteller", "Antragsgegnerin", "Kläger", "Beklagte", "Beschwerdeführer",
…) to the actual side. Be strict: only return winner/loser when the tenor
is unambiguous. When in doubt, return "unknown" — the user can flip it
manually.

Return JSON: {"client_role": "winner|loser|shared|each_own|unknown",
              "rationale": "<one short sentence quoting or paraphrasing the
              tenor>"}"""


def _excerpt_for_signal(signal: CostSignal, doc: Document) -> str | None:
    """Pick the best slice of text to feed the model.

    Preference order:
      1. Key passages on the source document marked ``kind == "ruling"`` —
         these are the AI-selected tenor quotes from the enricher pass.
      2. The leading slice of ``Document.content`` (markdown text).
    """
    passages = doc.key_passages or []
    ruling_quotes = [
        p.get("text", "").strip()
        for p in passages
        if isinstance(p, dict) and p.get("kind") == "ruling" and p.get("text")
    ]
    if ruling_quotes:
        joined = "\n\n".join(ruling_quotes)
        if joined.strip():
            return joined[:_MAX_CHARS]

    content = (doc.content or "").strip()
    if not content:
        return None
    return content[:_MAX_CHARS]


def detect_cost_ruling_role(signal_id: int, db: Session) -> dict | None:
    """Re-side a cost_ruling CostSignal using its source document text.

    Returns the new ``allocation`` dict (with ``client_role`` and
    ``auto_detected`` merged in) on success, or ``None`` if:
      - the signal doesn't exist,
      - it isn't a cost_ruling,
      - no source document text is available,
      - or the LLM came back with ``client_role == "unknown"`` (in which case
        the caller should preserve the existing allocation untouched).

    Caller is responsible for persisting + recomputing case exposure.
    """
    signal = db.get(CostSignal, signal_id)
    if not signal:
        return None
    if signal.signal_type.value != "cost_ruling":
        return None

    doc = db.get(Document, signal.source_document_id)
    if not doc:
        return None
    excerpt = _excerpt_for_signal(signal, doc)
    if not excerpt:
        return None

    party_identity = get_party_identity(db)
    case_opposing = (
        get_case_opposing_parties(signal.case_id, db) if signal.case_id else []
    )
    party_context = format_party_context(
        own_self=party_identity.get("own_self", ""),
        own_parties=party_identity.get("own_parties", []),
        opposing_parties=case_opposing,
    )

    cfg = get_chat_config(db)
    model = getattr(cfg, "summary_model", None)

    user_prompt = (
        f"{party_context}\n\n--- RULING EXCERPT ---\n{excerpt}"
        if party_context
        else f"--- RULING EXCERPT ---\n{excerpt}"
    )

    try:
        result = call_json_ai(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            options=_OPTIONS,
            debug_label=f"cost_ruling_sider_{signal_id}",
            schema=CostRulingSide,
            model=model or None,
            db=db,
            case_id=signal.case_id,
            two_pass=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cost_ruling_sider failed for signal %s: %s", signal_id, exc)
        return None

    if result.client_role == "unknown":
        logger.info(
            "cost_ruling_sider returned unknown for signal %s: %s",
            signal_id,
            result.rationale,
        )
        return None

    existing = dict(signal.allocation or {})

    # Translate the model's verdict into the same allocation shapes the
    # calculator already speaks. We deliberately do NOT invent fractions
    # for "shared" — the calculator falls back to existing own/opposing
    # values, defaulting to 50/50 when absent.
    if result.client_role == "each_own":
        new_alloc: dict = {"each_own": True, "client_role": "each_own"}
    elif result.client_role == "shared":
        new_alloc = {
            "own": existing.get("own", 0.5),
            "opposing": existing.get("opposing", 0.5),
            "client_role": "shared",
        }
    else:
        # winner / loser → the canonical "loser pays all" envelope; the
        # calculator interprets the side from client_role.
        new_alloc = {"loser": 1.0, "client_role": result.client_role}

    new_alloc["auto_detected"] = True
    if result.rationale:
        new_alloc["rationale"] = result.rationale[:280]

    return new_alloc

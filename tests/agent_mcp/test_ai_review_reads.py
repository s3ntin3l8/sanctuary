"""What an agent needs to judge the local AI: confidence, provenance, proposals,
OCR quality and entities, through the real MCP endpoint."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text as _sa_text

from app.models.database import (
    ActionItem,
    Claim,
    ClaimEvidence,
    ClaimEvidenceProposal,
    ClaimMergeProposal,
    Document,
    Entity,
)
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    ClaimEvidenceRole,
    ClaimStatus,
    EntityType,
    OriginatorType,
    ProposalConfidence,
    SignificanceTier,
)
from app.services.intelligence.brief_inputs import brief_inputs_hash

NOW = datetime.now(UTC)
FENCE = "<document-content>"


@pytest.fixture
def world(db_session, alice, make_case):
    """Alice's cases A-1 and A-2: a court relay in A-1 with graded metadata,
    OCR flags and stage provenance; claims, proposals and entities across both."""
    make_case("A-1", alice.id)
    make_case("A-2", alice.id)

    relay = Document(
        title="Weiterleitung Schriftsatz",
        content="text",
        case_id="A-1",
        owner_id=alice.id,
        originator_type=OriginatorType.COURT,
        significance_tier=SignificanceTier.CRITICAL,
        court_relay=True,
        sender="Amtsgericht Hamburg",
        attributed_originator="Kanzlei Müller",
        issued_date=datetime(2026, 1, 10, tzinfo=UTC),
        extraction_confidence={"significance_tier": "high", "sender": "email_header"},
        meta={
            "ai_contradiction": True,
            "contradiction_notes": ["Datum im Kopf weicht vom Stempel ab"],
            "extractor": "chandra-ocr-2",
            "chandra_ocr_error": "timeout",
            "page_failures": [3],
            "ai_context_strategy": "chunked",
        },
    )
    other = Document(
        title="Urteil",
        content="text",
        case_id="A-2",
        owner_id=alice.id,
        originator_type=OriginatorType.COURT,
        significance_tier=SignificanceTier.CRITICAL,
    )
    db_session.add_all([relay, other])
    db_session.flush()
    db_session.execute(
        _sa_text(
            "INSERT INTO document_pipeline_stages "
            "(document_id, stage, status, model, provider, prompt_version) VALUES "
            "(:d, 'enrich', 'completed', 'qwen3.5-9b', 'lmstudio', '2026-10-16.1')"
        ),
        {"d": relay.id},
    )

    own = Claim(claim_text="Die Frist ist gewahrt.", status=ClaimStatus.ASSERTED)
    foreign = Claim(claim_text="Die Frist ist versäumt.", status=ClaimStatus.ASSERTED)
    db_session.add_all([own, foreign])
    db_session.flush()
    db_session.add_all(
        [
            ClaimEvidence(
                claim_id=own.id, document_id=relay.id, role=ClaimEvidenceRole.ASSERTS
            ),
            ClaimEvidence(
                claim_id=foreign.id,
                document_id=other.id,
                role=ClaimEvidenceRole.ASSERTS,
            ),
            ClaimEvidenceProposal(
                target_claim_id=own.id,
                source_document_id=relay.id,
                proposed_role=ClaimEvidenceRole.SUPPORTS,
                excerpt="fristgerecht eingegangen",
                rationale="Eingangsstempel vor Fristende",
                confidence=ProposalConfidence.MEDIUM,
            ),
            ClaimMergeProposal(
                new_claim_id=own.id,
                existing_claim_id=foreign.id,
                confidence=ProposalConfidence.LOW,
                rationale="beide zur Frist",
            ),
            ActionItem(
                case_id="A-1",
                source_document_id=relay.id,
                title="Stellungnahme",
                due_date=NOW + timedelta(days=14),
                action_type=ActionItemType.DEADLINE,
                status=ActionItemStatus.OPEN,
                addressee="Beklagte",
            ),
            Entity(
                case_id="A-1",
                type=EntityType.LAW_FIRM,
                name="Kanzlei Müller",
                source_document_id=relay.id,
                extra_data={"context_quote": "Kanzlei Müller, Hamburg"},
            ),
            Entity(
                case_id="A-2",
                type=EntityType.COURT,
                name="Landgericht Hamburg",
                source_document_id=other.id,
            ),
        ]
    )
    db_session.commit()
    return {"relay": relay, "other": other, "own": own, "foreign": foreign}


def test_get_document_exposes_how_each_ai_output_was_decided(
    mcp_session, make_token, alice, world
):
    error, out = mcp_session(make_token(alice)).call(
        "get_document", document_id=world["relay"].id
    )
    assert not error

    meta = {m["field"]: m for m in out["metadata"]}
    assert meta["significance_tier"]["confidence"] == "high"
    # A grade is written by the AI and by the rule-based extractors alike, so
    # it names no source; only a recorded mark does.
    assert meta["significance_tier"]["source"] is None
    assert meta["received_date"]["source"] is None
    assert meta["sender"]["source"] == "email_header"
    assert out["sender"] == "Amtsgericht Hamburg"
    assert out["attributed_originator"] == "Kanzlei Müller"

    assert len(out["contradiction_notes"]) == 1
    assert FENCE in out["contradiction_notes"][0]

    ocr = out["processing"]["ocr"]
    assert ocr["engine"] == "chandra"
    assert ocr["fallback"] is True
    assert ocr["failed_pages"] == [3]
    assert out["processing"]["context_strategy"] == "chunked"
    enrich = next(s for s in out["processing"]["stages"] if s["stage"] == "enrich")
    assert (enrich["model"], enrich["provider"], enrich["prompt_version"]) == (
        "qwen3.5-9b",
        "lmstudio",
        "2026-10-16.1",
    )

    [proposal] = out["evidence_proposals"]
    assert proposal["claim_id"] == world["own"].id
    assert proposal["proposed_role"] == "supports"
    assert FENCE in proposal["rationale"]
    assert "Eingangsstempel vor Fristende" in proposal["rationale"]
    assert proposal["confidence"] == "medium"
    assert FENCE in proposal["excerpt"]


def test_document_ref_shows_the_relay_it_came_through(
    mcp_session, make_token, alice, world
):
    _, out = mcp_session(make_token(alice)).call(
        "get_document", document_id=world["relay"].id
    )
    assert out["document"]["sender"] == "Kanzlei Müller"
    assert out["document"]["routed_via"] == "Amtsgericht Hamburg"

    _, plain = mcp_session(make_token(alice)).call(
        "get_document", document_id=world["other"].id
    )
    assert plain["document"]["routed_via"] is None


def test_truth_map_lists_proposal_contents(mcp_session, make_token, alice, world):
    error, out = mcp_session(make_token(alice)).call("get_truth_map", case_id="A-1")
    assert not error
    [merge] = out["pending_merge_proposals"]
    assert (merge["new_claim_id"], merge["existing_claim_id"]) == (
        world["own"].id,
        world["foreign"].id,
    )
    assert FENCE in merge["rationale"] and "beide zur Frist" in merge["rationale"]
    [evidence] = out["pending_evidence_proposals"]
    assert evidence["proposed_role"] == "supports"
    assert evidence["confidence"] == "medium"
    assert FENCE in evidence["excerpt"]
    assert evidence["document"]["document_id"] == world["relay"].id


def test_truth_map_hides_a_merge_partner_outside_the_token(
    mcp_session, make_token, alice, world
):
    """The merge partner is evidenced only in A-2: an A-1 token must not read it."""
    scoped = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    _, out = scoped.call("get_truth_map", case_id="A-1")
    assert out["pending_merge_proposals"] == []
    assert len(out["pending_evidence_proposals"]) == 1


def test_list_action_items_carries_the_addressee(mcp_session, make_token, alice, world):
    _, items = mcp_session(make_token(alice)).call("list_action_items", case_id="A-1")
    assert items["result"][0]["addressee"] == "Beklagte"


def test_list_entities_by_case_and_by_document(mcp_session, make_token, alice, world):
    session = mcp_session(make_token(alice))
    _, by_case = session.call("list_entities", case_id="A-1")
    [entity] = by_case["result"]
    assert (entity["name"], entity["type"]) == ("Kanzlei Müller", "law_firm")
    assert FENCE in entity["context_quote"]
    assert entity["source_document"]["document_id"] == world["relay"].id

    _, by_doc = session.call("list_entities", document_id=world["other"].id)
    assert [e["name"] for e in by_doc["result"]] == ["Landgericht Hamburg"]


def test_list_entities_respects_the_token_scope(mcp_session, make_token, alice, world):
    scoped = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    error, msg = scoped.call("list_entities", case_id="A-2")
    assert error and "outside this token's scope" in msg
    error, msg = scoped.call("list_entities", document_id=world["other"].id)
    assert error and "outside this token's scope" in msg


@pytest.mark.parametrize(
    "arguments", [{}, {"case_id": "A-1", "document_id": 1}], ids=["none", "both"]
)
def test_list_entities_needs_exactly_one_target(
    mcp_session, make_token, alice, world, arguments
):
    error, msg = mcp_session(make_token(alice)).call("list_entities", **arguments)
    assert error and "exactly one" in msg


def test_get_case_reports_a_stale_brief(
    mcp_session, make_token, alice, world, db_session
):
    from app.models.database import Case

    case = db_session.get(Case, "A-1")
    case.ai_brief = {"posture": "p", "next_move": "n"}
    case.ai_brief_model = "qwen3.5-9b"
    case.ai_brief_prompt_version = "2026-10-16.1"
    case.ai_brief_inputs_hash = brief_inputs_hash(db_session, case)
    db_session.commit()

    session = mcp_session(make_token(alice))
    _, fresh = session.call("get_case", case_id="A-1")
    assert fresh["brief"]["stale"] is False
    assert fresh["brief"]["model"] == "qwen3.5-9b"

    world["relay"].significance_tier = SignificanceTier.INFORMATIONAL
    db_session.commit()
    _, stale = session.call("get_case", case_id="A-1")
    assert stale["brief"]["stale"] is True

    error, text = session.read_resource("sanctuary://case/A-1")
    assert not error and "**Stale:**" in text


def test_list_entities_drops_a_refiled_documents_entities_outside_the_token(
    mcp_session, make_token, alice, world, db_session
):
    """A document refiled after extraction leaves its entities in the old case:
    a token for that case must not read the quote of a document it cannot see."""
    world["relay"].case_id = "A-2"
    db_session.commit()
    scoped = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    error, out = scoped.call("list_entities", case_id="A-1")
    assert not error
    assert out["result"] == []

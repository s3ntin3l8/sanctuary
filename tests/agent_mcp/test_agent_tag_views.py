"""Rows written by an agent carry its token's name in every view; the user's own do not."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event

from app.api.v1.claims import _truth_map
from app.models.database import (
    ActionItem,
    ApiToken,
    Claim,
    ClaimEvidence,
    Document,
    DocumentPin,
    DocumentRelationship,
    UserReaction,
)
from app.models.enums import (
    ActionItemStatus,
    ActionItemType,
    ApiTokenAccess,
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    OriginatorType,
    RelationshipConfidence,
    RelationshipType,
    UserReactionType,
)
from app.services import (
    api_token_service,
    case_overview_service,
    document_review_service,
)

NOW = datetime.now(UTC)


def _result(payload):
    return (
        payload["result"]
        if isinstance(payload, dict) and "result" in payload
        else payload
    )


@pytest.fixture
def tagged(db_session, alice, make_case):
    """Case A-1 with one agent-written and one own row of every kind."""
    make_case("A-1", alice.id)
    token, secret = api_token_service.mint(
        db_session,
        alice,
        name="Claude Code",
        access=ApiTokenAccess.READ_WRITE,
        all_cases=True,
    )
    db_session.flush()

    def doc(title):
        d = Document(
            title=title,
            content="text",
            case_id="A-1",
            owner_id=alice.id,
            originator_type=OriginatorType.OPPOSING,
            issued_date=NOW,
            ingest_date=NOW,
        )
        db_session.add(d)
        db_session.flush()
        return d

    d1, d2, d3 = doc("Klage"), doc("Erwiderung"), doc("Replik")
    db_session.add_all(
        [
            UserReaction(
                document_id=d1.id,
                user_id=alice.id,
                reaction=UserReactionType.LIES,
                agent_token_id=token.id,
            ),
            DocumentPin(
                document_id=d1.id,
                user_id=alice.id,
                passage_id="abc",
                note="agent pin",
                agent_token_id=token.id,
            ),
            DocumentPin(
                document_id=d1.id, user_id=alice.id, passage_id="def", note="own pin"
            ),
            DocumentRelationship(
                from_document_id=d2.id,
                to_document_id=d1.id,
                relationship_type=RelationshipType.REPLIES_TO,
                confidence=RelationshipConfidence.USER_CREATED,
                agent_token_id=token.id,
            ),
            DocumentRelationship(
                from_document_id=d3.id,
                to_document_id=d1.id,
                relationship_type=RelationshipType.REFERENCES,
                confidence=RelationshipConfidence.USER_CREATED,
            ),
        ]
    )
    agent_claim = Claim(
        claim_text="Agent claim",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=NOW,
        last_updated_at=NOW,
        agent_token_id=token.id,
    )
    own_claim = Claim(
        claim_text="Own claim",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
        first_made_at=NOW,
        last_updated_at=NOW,
    )
    db_session.add_all([agent_claim, own_claim])
    db_session.flush()
    db_session.add_all(
        [
            ClaimEvidence(
                claim_id=agent_claim.id,
                document_id=d1.id,
                role=ClaimEvidenceRole.ASSERTS,
                agent_token_id=token.id,
            ),
            ClaimEvidence(
                claim_id=own_claim.id, document_id=d2.id, role=ClaimEvidenceRole.ASSERTS
            ),
            ActionItem(
                case_id="A-1",
                title="Agent deadline",
                due_date=NOW + timedelta(days=3),
                action_type=ActionItemType.DEADLINE,
                status=ActionItemStatus.OPEN,
                agent_token_id=token.id,
            ),
            ActionItem(
                case_id="A-1",
                title="Own deadline",
                due_date=NOW + timedelta(days=4),
                action_type=ActionItemType.DEADLINE,
                status=ActionItemStatus.OPEN,
            ),
        ]
    )
    db_session.commit()
    return {"token": token, "secret": secret, "d1": d1, "d2": d2, "d3": d3}


def test_token_names_is_one_query_and_skips_empty_input(db_session, alice, tagged):
    tid = tagged["token"].id
    statements: list[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert api_token_service.token_names(db_session, [tid, tid, None, tid]) == {
            tid: "Claude Code"
        }
        assert api_token_service.token_names(db_session, [None, None]) == {}
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) == 1 and "api_tokens" in statements[0]


def test_reader_view_tags_agent_rows_only(db_session, alice, tagged):
    reader = document_review_service.reader_view(db_session, alice, tagged["d1"])
    assert [r.agent_token_name for r in reader.reactions] == ["Claude Code"]
    assert {p.note: p.agent_token_name for p in reader.pins} == {
        "agent pin": "Claude Code",
        "own pin": None,
    }
    assert {r.rel_type: r.agent_token_name for r in reader.relationships} == {
        RelationshipType.REPLIES_TO: "Claude Code",
        RelationshipType.REFERENCES: None,
    }


def test_truth_map_tags_claims_and_evidence(db_session, alice, tagged):
    view = _truth_map(db_session, alice, "A-1", "all")
    claims = {c.claim_text: c for g in view.groups for c in g.claims}
    assert claims["Agent claim"].agent_token_name == "Claude Code"
    assert claims["Agent claim"].evidence[0].agent_token_name == "Claude Code"
    assert claims["Own claim"].agent_token_name is None
    assert claims["Own claim"].evidence[0].agent_token_name is None


def test_case_overview_tags_action_items(db_session, alice, tagged):
    from app.models.database import Case

    case = db_session.get(Case, "A-1")
    detail = case_overview_service.build_case_detail(
        db_session, alice, case, active_proceeding_id=None, last_visit=None
    )
    assert {a.title: a.agent_token_name for a in detail.action_items} == {
        "Agent deadline": "Claude Code",
        "Own deadline": None,
    }


def test_a_deleted_token_leaves_a_null_name(db_session, alice, tagged):
    db_session.query(ApiToken).filter_by(id=tagged["token"].id).delete()
    db_session.commit()
    db_session.expire_all()
    reader = document_review_service.reader_view(db_session, alice, tagged["d1"])
    assert [r.agent_token_name for r in reader.reactions] == [None]


def test_mcp_read_tools_expose_the_tag(mcp_session, tagged):
    session = mcp_session(tagged["secret"])

    error, doc = session.call("get_document", document_id=tagged["d1"].id)
    assert not error
    assert doc["your_reactions"][0]["agent_token_name"] == "Claude Code"
    assert {p["note"]: p["agent_token_name"] for p in doc["your_pins"]} == {
        "agent pin": "Claude Code",
        "own pin": None,
    }
    assert "Claude Code" in {r["agent_token_name"] for r in doc["relationships"]}

    error, tm = session.call("get_truth_map", case_id="A-1", status="all")
    claims = {c["text"]: c for c in _result(tm)["claims"]}
    assert claims["Agent claim"]["agent_token_name"] == "Claude Code"
    assert claims["Agent claim"]["evidence"][0]["agent_token_name"] == "Claude Code"
    assert claims["Own claim"]["agent_token_name"] is None

    error, items = session.call("list_action_items", case_id="A-1")
    assert {i["title"]: i["agent_token_name"] for i in _result(items)} == {
        "Agent deadline": "Claude Code",
        "Own deadline": None,
    }

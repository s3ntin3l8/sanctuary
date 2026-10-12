"""MCP write tools: scope, provenance tagging, audit and the re-enrichment trap."""

from datetime import UTC, datetime

import pytest

from app.models.database import (
    ActionItem,
    AuditLog,
    CaseShare,
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
    AuditEventType,
    CaseAccessLevel,
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    RelationshipConfidence,
    RelationshipType,
    UserReactionType,
)
from app.repositories.action_item import ActionItemRepository
from app.repositories.claim import ClaimRepository
from app.repositories.claim_evidence import ClaimEvidenceRepository
from app.repositories.document_relationship import reject_edge
from app.repositories.user_reaction import UserReactionRepository
from app.services.intelligence import action_items as action_item_extraction

RW = ApiTokenAccess.READ_WRITE


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    """The embedding provider is not available in tests; the lookups are best effort."""

    async def nothing(*args, **kwargs):
        return []

    async def no_embedding(*args, **kwargs):
        return False

    monkeypatch.setattr("app.mcp.tools_write.nearest_claims", nothing)
    monkeypatch.setattr("app.mcp.tools_write.upsert_claim_embedding", no_embedding)


@pytest.fixture
def make_doc(db_session):
    def _make(title, *, owner, case_id, **kw):
        doc = Document(
            title=title,
            owner_id=owner.id,
            case_id=case_id,
            issued_date=datetime(2026, 3, 1, tzinfo=UTC),
            content=f"text of {title}",
            **kw,
        )
        db_session.add(doc)
        db_session.commit()
        return doc

    return _make


@pytest.fixture
def make_claim(db_session):
    def _make(doc, text="The deadline was missed", status=ClaimStatus.ASSERTED):
        claim = ClaimRepository(db_session).create_claim(
            text, ClaimType.FACTUAL, status=status
        )
        ClaimEvidenceRepository(db_session).link(
            claim.id,
            doc.id,
            ClaimEvidenceRole.ASSERTS,
            "excerpt",
            confidence=RelationshipConfidence.USER_CREATED,
        )
        db_session.commit()
        return claim

    return _make


def _ok(session, tool, **args):
    error, out = session.call(tool, **args)
    assert not error, out
    return out


def _fails(session, tool, **args) -> str:
    error, out = session.call(tool, **args)
    assert error, out
    return out


def _audits(db):
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.event_type == AuditEventType.MCP_WRITE)


def _token_id(db):
    from app.models.database import ApiToken

    return db.query(ApiToken).order_by(ApiToken.id.desc()).first().id


# --- scope -------------------------------------------------------------------


@pytest.fixture
def world(db_session, alice, make_case, make_doc, make_claim):
    make_case("A-1", alice.id)
    d1 = make_doc("Letter", owner=alice, case_id="A-1")
    d2 = make_doc("Reply", owner=alice, case_id="A-1")
    claim = make_claim(d1)
    item = ActionItemRepository(db_session).create_action_item(
        "A-1", "File answer", datetime(2026, 5, 1), ActionItemType.DEADLINE
    )
    db_session.commit()
    return {"d1": d1.id, "d2": d2.id, "claim": claim.id, "item": item.id}


def _every_write_call(w):
    return [
        ("set_reaction", {"document_id": w["d1"], "reaction": "lies"}),
        ("add_pin", {"document_id": w["d1"], "passage_id": "x", "note": "n"}),
        (
            "create_claim",
            {
                "case_id": "A-1",
                "text": "Something happened",
                "claim_type": "factual",
                "document_id": w["d1"],
            },
        ),
        (
            "link_claim_evidence",
            {"claim_id": w["claim"], "document_id": w["d2"], "role": "contests"},
        ),
        ("set_claim_status", {"claim_id": w["claim"], "status": "established"}),
        (
            "create_action_item",
            {
                "case_id": "A-1",
                "title": "t",
                "due_date": "2026-06-01",
                "action_type": "deadline",
            },
        ),
        ("update_action_item", {"action_item_id": w["item"], "status": "completed"}),
        (
            "create_relationship",
            {
                "from_document_id": w["d2"],
                "to_document_id": w["d1"],
                "relationship_type": "replies_to",
            },
        ),
    ]


def test_every_write_tool_rejects_a_read_only_token(
    mcp_session, make_token, alice, world
):
    session = mcp_session(make_token(alice, access=ApiTokenAccess.READ))
    for tool, args in _every_write_call(world):
        assert "read-only" in _fails(session, tool, **args), tool


def test_a_viewer_share_cannot_be_written_through_a_read_write_token(
    mcp_session, make_token, make_case, make_doc, db_session, alice, bob
):
    make_case("B-1", bob.id)
    doc = make_doc("Bob's letter", owner=bob, case_id="B-1")
    db_session.add(
        CaseShare(case_id="B-1", user_id=alice.id, permission=CaseAccessLevel.VIEWER)
    )
    db_session.commit()
    session = mcp_session(make_token(alice, access=RW))
    assert not session.call("get_document", document_id=doc.id)[0]
    message = _fails(session, "set_reaction", document_id=doc.id, reaction="lies")
    assert "not edit" in message
    assert "not edit" in _fails(
        session,
        "create_action_item",
        case_id="B-1",
        title="t",
        due_date="2026-06-01",
        action_type="deadline",
    )


def test_a_per_case_token_cannot_write_another_case(
    mcp_session, make_token, make_case, make_doc, alice, world
):
    make_case("A-2", alice.id)
    other = make_doc("Elsewhere", owner=alice, case_id="A-2")
    session = mcp_session(
        make_token(alice, access=RW, all_cases=False, case_ids=["A-1"])
    )
    assert "outside" in _fails(
        session, "set_reaction", document_id=other.id, reaction="lies"
    )
    assert "outside" in _fails(
        session,
        "create_action_item",
        case_id="A-2",
        title="t",
        due_date="2026-06-01",
        action_type="deadline",
    )
    assert _ok(session, "set_reaction", document_id=world["d1"], reaction="lies")


def test_another_users_claim_is_invisible(
    mcp_session, make_token, make_case, make_doc, make_claim, bob, alice
):
    make_case("B-1", bob.id)
    claim = make_claim(make_doc("Bob's", owner=bob, case_id="B-1"))
    session = mcp_session(make_token(alice, access=RW))
    assert "outside" in _fails(
        session, "set_claim_status", claim_id=claim.id, status="established"
    )


# --- annotations -------------------------------------------------------------


def test_set_reaction_tags_the_row_and_audits(
    mcp_session, make_token, db_session, alice, world
):
    session = mcp_session(make_token(alice, access=RW))
    out = _ok(
        session, "set_reaction", document_id=world["d1"], reaction="lies", notes="why"
    )["result"]
    assert out == [{"reaction": "lies", "notes": "why", "by_agent": True}]
    db_session.expire_all()
    row = db_session.query(UserReaction).one()
    assert row.user_id == alice.id and row.agent_token_id == _token_id(db_session)
    audit = _audits(db_session).one()
    assert audit.actor_label == "mcp:agent" and audit.actor_user_id == alice.id
    assert audit.payload["tool"] == "set_reaction"
    assert audit.payload["api_token_id"] == row.agent_token_id


def test_set_reaction_never_removes_the_users_own_reaction(
    mcp_session, make_token, db_session, alice, world
):
    UserReactionRepository(db_session).set_reaction(
        world["d1"], UserReactionType.LIES, None, user_id=alice.id
    )
    db_session.commit()
    session = mcp_session(make_token(alice, access=RW))
    out = _ok(session, "set_reaction", document_id=world["d1"], reaction="lies")
    assert out["result"] == [{"reaction": "lies", "notes": None, "by_agent": False}]
    out = _ok(
        session, "set_reaction", document_id=world["d1"], reaction="lies", notes="n"
    )
    assert out["result"][0]["by_agent"] is True  # notes edit re-tags the row


def test_add_pin(mcp_session, make_token, db_session, alice, world):
    doc = db_session.get(Document, world["d1"])
    doc.key_passages = [{"id": "p1", "text": "the key sentence", "kind": "issue"}]
    db_session.commit()
    session = mcp_session(make_token(alice, access=RW))
    assert "key passages" in _fails(
        session, "add_pin", document_id=doc.id, passage_id="nope", note="n"
    )
    out = _ok(session, "add_pin", document_id=doc.id, passage_id="p1", note="check")
    db_session.expire_all()
    pin = db_session.query(DocumentPin).one()
    assert pin.id == out["pin_id"] and pin.note == "check"
    assert pin.user_id == alice.id and pin.agent_token_id == _token_id(db_session)
    assert _audits(db_session).one().payload["tool"] == "add_pin"


# --- Truth Map ---------------------------------------------------------------


def test_create_claim_writes_the_asserts_evidence(
    mcp_session, make_token, db_session, alice, world
):
    session = mcp_session(make_token(alice, access=RW))
    out = _ok(
        session,
        "create_claim",
        case_id="A-1",
        text="  The notice arrived late  ",
        claim_type="procedural",
        document_id=world["d2"],
        excerpt="arrived on 3 March",
    )
    assert out["status"] == "asserted" and out["similar_claims"] == []
    db_session.expire_all()
    claim = db_session.get(Claim, out["claim_id"])
    assert claim.claim_text == "The notice arrived late"
    assert claim.agent_token_id == _token_id(db_session)
    ev = db_session.query(ClaimEvidence).filter_by(claim_id=claim.id).one()
    assert ev.role == ClaimEvidenceRole.ASSERTS and ev.document_id == world["d2"]
    assert ev.confidence == RelationshipConfidence.USER_CREATED
    assert ev.excerpt == "arrived on 3 March"
    assert ev.agent_token_id == claim.agent_token_id
    assert _audits(db_session).one().payload["tool"] == "create_claim"


def test_create_claim_reports_similar_claims_without_blocking(
    mcp_session, make_token, db_session, alice, world, monkeypatch
):
    async def near(text, db, *, k, case_id):
        return [(world["claim"], 0.12345)]

    monkeypatch.setattr("app.mcp.tools_write.nearest_claims", near)
    session = mcp_session(make_token(alice, access=RW))
    out = _ok(
        session,
        "create_claim",
        case_id="A-1",
        text="The deadline was missed again",
        claim_type="factual",
        document_id=world["d1"],
    )
    assert out["similar_claims"] == [
        {
            "claim_id": world["claim"],
            "text": "The deadline was missed",
            "status": "asserted",
            "distance": 0.1235,
        }
    ]
    assert db_session.query(Claim).count() == 2


def test_create_claim_needs_a_document_of_that_case(
    mcp_session, make_token, make_case, make_doc, alice, world
):
    make_case("A-2", alice.id)
    other = make_doc("Elsewhere", owner=alice, case_id="A-2")
    session = mcp_session(make_token(alice, access=RW))
    assert "not in this case" in _fails(
        session,
        "create_claim",
        case_id="A-1",
        text="Something happened",
        claim_type="factual",
        document_id=other.id,
    )


def test_link_claim_evidence_moves_status_and_keeps_the_originating_row(
    mcp_session, make_token, db_session, alice, world
):
    session = mcp_session(make_token(alice, access=RW))
    out = _ok(
        session,
        "link_claim_evidence",
        claim_id=world["claim"],
        document_id=world["d2"],
        role="contests",
        excerpt="we deny",
    )
    assert out["changed"] is True and out["claim_status"] == "contested"
    db_session.expire_all()
    ev = db_session.query(ClaimEvidence).filter_by(document_id=world["d2"]).one()
    assert ev.role == ClaimEvidenceRole.CONTESTS
    assert ev.confidence == RelationshipConfidence.USER_CREATED
    assert ev.agent_token_id == _token_id(db_session)

    # The asserting document's row is never rewritten.
    again = _ok(
        session,
        "link_claim_evidence",
        claim_id=world["claim"],
        document_id=world["d1"],
        role="refutes",
    )
    assert again["changed"] is False and again["role"] == "asserts"
    # A second link from the same document replaces, not duplicates.
    _ok(
        session,
        "link_claim_evidence",
        claim_id=world["claim"],
        document_id=world["d2"],
        role="supports",
    )
    db_session.expire_all()
    assert (
        db_session.query(ClaimEvidence).filter_by(document_id=world["d2"]).count() == 1
    )


def test_set_claim_status_follows_the_allowed_transitions(
    mcp_session, make_token, db_session, alice, world
):
    session = mcp_session(make_token(alice, access=RW))
    out = _ok(
        session, "set_claim_status", claim_id=world["claim"], status="established"
    )
    assert out["status"] == "established"
    assert "Cannot transition" in _fails(
        session, "set_claim_status", claim_id=world["claim"], status="established"
    )
    assert _audits(db_session).count() == 1


# --- deadlines ---------------------------------------------------------------


def test_create_action_item_and_conflict(
    mcp_session, make_token, db_session, alice, world
):
    session = mcp_session(make_token(alice, access=RW))
    args = {
        "case_id": "A-1",
        "title": "Respond to the court",
        "due_date": "2026-06-15T10:00:00+02:00",
        "action_type": "response_required",
        "description": "at 10:00",
        "source_document_id": world["d1"],
    }
    out = _ok(session, "create_action_item", **args)
    assert out["due_date"] == "2026-06-15" and out["status"] == "open"
    db_session.expire_all()
    item = db_session.get(ActionItem, out["action_item_id"])
    assert item.agent_token_id == _token_id(db_session)
    assert item.source_document_id == world["d1"]
    message = _fails(session, "create_action_item", **args)
    assert "already exists" in message and str(item.id) in message
    assert "ISO date" in _fails(
        session, "create_action_item", **{**args, "due_date": "soon"}
    )
    assert _audits(db_session).count() == 1


def test_update_action_item(mcp_session, make_token, db_session, alice, world):
    session = mcp_session(make_token(alice, access=RW))
    out = _ok(
        session, "update_action_item", action_item_id=world["item"], status="completed"
    )
    assert out["status"] == "completed"
    db_session.expire_all()
    assert (
        db_session.get(ActionItem, world["item"]).status == ActionItemStatus.COMPLETED
    )
    assert _audits(db_session).one().payload["to_status"] == "completed"


def test_re_enrichment_never_purges_an_agent_deadline(
    mcp_session, make_token, db_session, alice, world
):
    session = mcp_session(make_token(alice, access=RW))
    mine = _ok(
        session,
        "create_action_item",
        case_id="A-1",
        title="Agent deadline",
        due_date="2026-07-01",
        action_type="deadline",
        source_document_id=world["d1"],
    )["action_item_id"]
    ai_item = ActionItemRepository(db_session).create_action_item(
        "A-1",
        "AI deadline",
        datetime(2026, 8, 1),
        ActionItemType.DEADLINE,
        source_document_id=world["d1"],
    )
    db_session.commit()
    ai_id = ai_item.id

    # The enricher's "not a court source any more" purge ...
    assert (
        action_item_extraction.purge_action_items_from_doc(world["d1"], db_session) == 1
    )
    db_session.commit()
    assert db_session.get(ActionItem, mine) is not None
    assert db_session.get(ActionItem, ai_id) is None

    # ... and a re-extraction that replaces the document's items.
    action_item_extraction.create_from_payload(
        "A-1",
        world["d1"],
        None,
        [{"title": "Fresh", "due_date": "2026-09-01", "action_type": "deadline"}],
        db_session,
    )
    db_session.commit()
    assert db_session.get(ActionItem, mine) is not None


# --- document links ----------------------------------------------------------


def test_create_relationship_is_idempotent_and_user_created(
    mcp_session, make_token, db_session, alice, world
):
    session = mcp_session(make_token(alice, access=RW))
    args = {
        "from_document_id": world["d2"],
        "to_document_id": world["d1"],
        "relationship_type": "replies_to",
        "notes": "answers the letter",
    }
    first = _ok(session, "create_relationship", **args)
    assert first["created"] is True
    db_session.expire_all()
    edge = db_session.get(DocumentRelationship, first["relationship_id"])
    assert edge.confidence == RelationshipConfidence.USER_CREATED
    assert edge.agent_token_id == _token_id(db_session)
    assert edge.notes == "answers the letter"

    second = _ok(session, "create_relationship", **args)
    assert second["created"] is False
    assert second["relationship_id"] == first["relationship_id"]
    assert db_session.query(DocumentRelationship).count() == 1
    assert _audits(db_session).count() == 1


def test_a_rejected_link_is_not_recreated(
    mcp_session, make_token, db_session, alice, world
):
    session = mcp_session(make_token(alice, access=RW))
    args = {
        "from_document_id": world["d2"],
        "to_document_id": world["d1"],
        "relationship_type": "references",
    }
    edge_id = _ok(session, "create_relationship", **args)["relationship_id"]
    db_session.expire_all()
    reject_edge(db_session, db_session.get(DocumentRelationship, edge_id))
    db_session.commit()
    assert "rejected" in _fails(session, "create_relationship", **args)
    assert db_session.query(DocumentRelationship).count() == 0


def test_create_relationship_validates_its_documents(
    mcp_session, make_token, make_case, make_doc, alice, world
):
    make_case("A-2", alice.id)
    other = make_doc("Elsewhere", owner=alice, case_id="A-2")
    session = mcp_session(make_token(alice, access=RW))
    assert "itself" in _fails(
        session,
        "create_relationship",
        from_document_id=world["d1"],
        to_document_id=world["d1"],
        relationship_type="references",
    )
    assert "same case" in _fails(
        session,
        "create_relationship",
        from_document_id=world["d1"],
        to_document_id=other.id,
        relationship_type="references",
    )
    assert RelationshipType.REFERENCES.value == "references"

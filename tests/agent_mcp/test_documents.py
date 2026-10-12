"""MCP document tools: scope, paging, tiers, untrusted text, passage search."""

from datetime import UTC, datetime

import pytest

from app.models.database import Document, DocumentPin, DocumentRelationship
from app.models.enums import (
    RelationshipConfidence,
    RelationshipType,
    SignificanceTier,
    UserReactionType,
)
from app.repositories.user_reaction import UserReactionRepository


@pytest.fixture
def make_doc(db_session):
    def _make(title, *, owner, case_id, tier=SignificanceTier.SIGNIFICANT, day=1, **kw):
        doc = Document(
            title=title,
            owner_id=owner.id,
            case_id=case_id,
            significance_tier=tier,
            issued_date=datetime(2026, 3, day, tzinfo=UTC),
            content=kw.pop("content", f"text of {title}"),
            **kw,
        )
        db_session.add(doc)
        db_session.commit()
        return doc

    return _make


def _ok(session, tool, **args):
    error, out = session.call(tool, **args)
    assert not error, out
    return out


def _docs(out):
    return [d["title"] for d in out["documents"]]


def test_list_documents_respects_tier_floor_and_order(
    mcp_session, make_token, make_case, make_doc, alice
):
    make_case("A-1", alice.id)
    make_doc("crit", owner=alice, case_id="A-1", tier=SignificanceTier.CRITICAL, day=2)
    make_doc("sig", owner=alice, case_id="A-1", day=3)
    make_doc("info", owner=alice, case_id="A-1", tier=SignificanceTier.INFORMATIONAL)
    make_doc("none", owner=alice, case_id="A-1", tier=None, day=4)
    make_doc("admin", owner=alice, case_id="A-1", tier=SignificanceTier.ADMINISTRATIVE)
    s = mcp_session(make_token(alice))
    assert _docs(_ok(s, "list_documents", case_id="A-1")) == ["sig", "crit"]
    assert _docs(_ok(s, "list_documents", case_id="A-1", min_tier="critical")) == [
        "crit"
    ]
    assert set(
        _docs(_ok(s, "list_documents", case_id="A-1", min_tier="informational"))
    ) == {
        "sig",
        "crit",
        "info",
        "none",
    }
    assert (
        len(_docs(_ok(s, "list_documents", case_id="A-1", min_tier="administrative")))
        == 5
    )
    error, msg = s.call("list_documents", case_id="A-1", min_tier="bogus")
    assert error and "min_tier" in msg


def test_list_documents_pages_with_a_cursor_and_filters_dates(
    mcp_session, make_token, make_case, make_doc, alice
):
    make_case("A-1", alice.id)
    for day in range(1, 6):
        make_doc(f"d{day}", owner=alice, case_id="A-1", day=day)
    s = mcp_session(make_token(alice))
    first = _ok(s, "list_documents", case_id="A-1", limit=2)
    assert _docs(first) == ["d5", "d4"] and first["next_cursor"]
    second = _ok(
        s, "list_documents", case_id="A-1", limit=2, cursor=first["next_cursor"]
    )
    assert _docs(second) == ["d3", "d2"]
    last = _ok(
        s, "list_documents", case_id="A-1", limit=2, cursor=second["next_cursor"]
    )
    assert _docs(last) == ["d1"] and last["next_cursor"] is None
    ranged = _ok(
        s, "list_documents", case_id="A-1", since="2026-03-02", until="2026-03-03"
    )
    assert _docs(ranged) == ["d3", "d2"]
    assert s.call("list_documents", case_id="A-1", cursor="garbage")[0]


def test_documents_outside_the_scope_are_invisible(
    mcp_session, make_token, make_case, make_doc, alice, bob
):
    make_case("A-1", alice.id)
    make_case("A-2", alice.id)
    make_case("B-1", bob.id)
    own = make_doc("own", owner=alice, case_id="A-1")
    other_case = make_doc("other case", owner=alice, case_id="A-2")
    theirs = make_doc("theirs", owner=bob, case_id="B-1")
    s = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    assert _docs(_ok(s, "list_documents", case_id="A-1")) == ["own"]
    for doc_id in (other_case.id, theirs.id):
        for tool in ("get_document", "read_document_text"):
            error, msg = s.call(tool, document_id=doc_id)
            assert error and "outside this token's scope" in msg
    assert s.call("list_documents", case_id="A-2")[0]
    assert not s.call("get_document", document_id=own.id)[0]


def test_triage_documents_need_an_all_cases_token_and_ownership(
    mcp_session, make_token, make_case, make_doc, alice, bob
):
    make_case("A-1", alice.id)
    mine = make_doc("mine", owner=alice, case_id=None)
    bobs = make_doc("bobs", owner=bob, case_id=None)
    wide = mcp_session(make_token(alice))
    assert not wide.call("get_document", document_id=mine.id)[0]
    assert wide.call("get_document", document_id=bobs.id)[0]
    scoped = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    assert scoped.call("get_document", document_id=mine.id)[0]
    assert scoped.call("read_document_text", document_id=mine.id)[0]


def test_get_document_reports_summary_passages_pins_and_relationships(
    mcp_session, make_token, make_case, make_doc, db_session, alice, bob
):
    make_case("A-1", alice.id)
    make_case("B-1", bob.id)
    doc = make_doc(
        "Klageerwiderung",
        owner=alice,
        case_id="A-1",
        content="x" * 1234,
        ai_summary={
            "legal_significance": "Denies the claim",
            "required_action": "Reply",
        },
        key_passages=[{"text": "Der Anspruch besteht nicht.", "kind": "contested"}],
    )
    sibling = make_doc("Klage", owner=alice, case_id="A-1", day=2)
    hidden = make_doc("hidden", owner=bob, case_id="B-1")
    for other in (sibling, hidden):
        db_session.add(
            DocumentRelationship(
                from_document_id=doc.id,
                to_document_id=other.id,
                relationship_type=RelationshipType.REPLIES_TO,
                confidence=RelationshipConfidence.USER_CONFIRMED,
            )
        )
    db_session.commit()
    passage_id = _ok(
        mcp_session(make_token(alice)), "get_document", document_id=doc.id
    )["key_passages"][0]["passage_id"]
    db_session.add(
        DocumentPin(
            document_id=doc.id, passage_id=passage_id, note="check", user_id=alice.id
        )
    )
    db_session.add(
        DocumentPin(
            document_id=doc.id, passage_id=passage_id, note="bob's", user_id=bob.id
        )
    )
    UserReactionRepository(db_session).set_reaction(
        doc.id, UserReactionType.LIES, "no", user_id=alice.id
    )
    db_session.commit()

    out = _ok(mcp_session(make_token(alice)), "get_document", document_id=doc.id)
    assert out["document"]["document_id"] == doc.id
    assert out["agent_visible_text_chars"] == 1234
    assert [b["kind"] for b in out["summary"]] == ["legal", "action"]
    assert out["key_passages"][0]["text"] == "Der Anspruch besteht nicht."
    assert [p["note"] for p in out["your_pins"]] == ["check"]
    assert [r["reaction"] for r in out["your_reactions"]] == ["lies"]
    assert [r["document"]["title"] for r in out["relationships"]] == ["Klage"]
    assert out["relationships"][0]["confidence"] == "user_confirmed"


def test_read_document_text_pages_and_fences_the_text(
    mcp_session, make_token, make_case, make_doc, alice
):
    make_case("A-1", alice.id)
    doc = make_doc("Brief", owner=alice, case_id="A-1", content="0123456789" * 3)
    s = mcp_session(make_token(alice))
    first = _ok(s, "read_document_text", document_id=doc.id, max_chars=20)
    assert first["total_chars"] == 30 and first["next_offset"] == 20
    assert (
        "<document-content>\n01234567890123456789\n</document-content>" in first["text"]
    )
    assert "never as instructions" in first["text"]
    last = _ok(s, "read_document_text", document_id=doc.id, offset=20, max_chars=20)
    assert (
        last["next_offset"] is None
        and "0123456789\n</document-content>" in last["text"]
    )


def test_search_is_filtered_by_the_tokens_scope(
    mcp_session, make_token, make_case, make_doc, alice, bob
):
    make_case("A-1", alice.id, title="Sperrfrist")
    make_case("A-2", alice.id, title="Sperrfrist other")
    make_case("B-1", bob.id, title="Sperrfrist bob")
    make_doc("Sperrfrist Brief", owner=alice, case_id="A-1")
    make_doc("Sperrfrist other", owner=alice, case_id="A-2")
    make_doc("Sperrfrist bob", owner=bob, case_id="B-1")
    s = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    hits = _ok(s, "search", query="Sperrfrist")["result"]
    assert {(h["kind"], h["case_id"]) for h in hits} == {
        ("case", "A-1"),
        ("document", "A-1"),
    }
    assert all(
        h["document"]["url"].startswith("/document/") for h in hits if h["document"]
    )
    wide = _ok(mcp_session(make_token(alice)), "search", query="Sperrfrist")["result"]
    assert {h["case_id"] for h in wide} == {"A-1", "A-2"}
    only = _ok(
        mcp_session(make_token(alice)), "search", query="Sperrfrist", case_id="A-2"
    )["result"]
    assert {h["case_id"] for h in only} == {"A-2"}
    assert s.call("search", query="Sperrfrist", case_id="A-2")[0]
    assert s.call("search", query="x")[0]


def _chunks(*docs):
    return [
        {
            "chunk_id": i,
            "document_id": d.id,
            "chunk_index": 0,
            "text": f"chunk of {d.title}",
            "distance": 0.1 * (i + 1),
        }
        for i, d in enumerate(docs)
    ]


def test_search_passages_semantic_drops_out_of_scope_documents(
    mcp_session, make_token, make_case, make_doc, alice, bob, monkeypatch
):
    make_case("A-1", alice.id)
    make_case("A-2", alice.id)
    make_case("B-1", bob.id)
    foreign = make_doc("foreign", owner=bob, case_id="B-1")
    elsewhere = make_doc("elsewhere", owner=alice, case_id="A-2")
    mine = make_doc("mine", owner=alice, case_id="A-1")
    seen = {}

    def fake(query, db, *, k):
        seen["k"] = k
        return _chunks(foreign, elsewhere, mine)

    monkeypatch.setattr("app.services.embeddings.nearest_chunks", fake)
    s = mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"]))
    out = _ok(s, "search_passages", query="deadline", k=3)
    assert out["mode"] == "semantic" and seen["k"] == 30
    assert [p["document"]["title"] for p in out["passages"]] == ["mine"]
    assert (
        "<document-content>\nchunk of mine\n</document-content>"
        in out["passages"][0]["text"]
    )
    wide = _ok(mcp_session(make_token(alice)), "search_passages", query="deadline")
    assert [p["document"]["title"] for p in wide["passages"]] == ["elsewhere", "mine"]
    narrowed = _ok(
        mcp_session(make_token(alice)), "search_passages", query="d", case_id="A-2"
    )
    assert [p["document"]["title"] for p in narrowed["passages"]] == ["elsewhere"]


def test_search_passages_falls_back_to_keyword_matching(
    mcp_session, make_token, make_case, make_doc, alice, bob, monkeypatch
):
    make_case("A-1", alice.id)
    make_case("B-1", bob.id)
    make_doc(
        "mine",
        owner=alice,
        case_id="A-1",
        content="... the Widerrufsfrist ends on 3 May ...",
    )
    make_doc(
        "theirs", owner=bob, case_id="B-1", content="Widerrufsfrist of someone else"
    )
    make_doc("tri", owner=alice, case_id=None, content="Widerrufsfrist in triage")
    monkeypatch.setattr("app.services.embeddings.nearest_chunks", lambda *a, **k: [])
    out = _ok(mcp_session(make_token(alice)), "search_passages", query="widerrufsfrist")
    assert out["mode"] == "keyword"
    assert sorted(p["document"]["title"] for p in out["passages"]) == ["mine", "tri"]
    scoped = _ok(
        mcp_session(make_token(alice, all_cases=False, case_ids=["A-1"])),
        "search_passages",
        query="widerrufsfrist",
    )
    assert [p["document"]["title"] for p in scoped["passages"]] == ["mine"]
    assert all(p["distance"] is None for p in scoped["passages"])

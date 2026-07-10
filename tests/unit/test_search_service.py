"""Smoke tests for SearchService — the audit flagged it as having zero coverage.

Three concerns pinned here:
1. `search_all` returns ilike hits across multiple entity types from a single query.
2. `_semantic_document_ids` swallows provider failures and returns `[]` (so a
   misconfigured embedding model never raises into the user's search).
3. `semantic_document_search` orders documents by the ID order returned from
   the vector query (lowest distance first).
"""

from datetime import datetime
from unittest.mock import patch

import pytest

from app.models.database import (
    ActionItem,
    Case,
    Document,
    IngestBatch,
    LegalCost,
)
from app.models.enums import (
    ActionItemType,
    CaseStatus,
    CostCategory,
    CostStatus,
    IngestBatchSourceType,
)
from app.services.search_service import SearchService


@pytest.fixture
def seeded(db_session):
    case = Case(id="SRCH-1", title="Unterhaltsforderung", status=CaseStatus.INTAKE)
    db_session.add(case)
    batch = IngestBatch(source_type=IngestBatchSourceType.MANUAL)
    db_session.add(batch)
    db_session.commit()

    doc = Document(
        title="Schreiben Unterhaltsanpassung",
        content="Mandant fordert Unterhaltsanpassung gemäß § 1612 BGB",
        ingest_batch_id=batch.id,
        case_id=case.id,
    )
    db_session.add(doc)
    db_session.add(
        ActionItem(
            case_id=case.id,
            title="Unterhaltsfrist",
            action_type=ActionItemType.DEADLINE,
            due_date=datetime(2026, 6, 1),
        )
    )
    db_session.add(
        LegalCost(
            case_id=case.id,
            category=CostCategory.GERICHTSKOSTEN,
            title="GK Unterhaltsverfahren",
            amount_net=100,
            amount_gross=100,
            status=CostStatus.OFFEN,
        )
    )
    db_session.commit()
    return db_session


@pytest.mark.unit
def test_search_all_finds_across_types(seeded):
    result = SearchService(seeded).search_all("unterhalt", limit=30)
    assert any(c.id == "SRCH-1" for c in result.cases)
    assert any("Unterhalts" in d.title for d in result.documents)
    assert any("Unterhalts" in a.title for a in result.deadlines)
    assert any("Unterhalts" in c.title for c in result.costs)
    assert result.total >= 4


@pytest.mark.unit
def test_search_all_empty_query_no_match(seeded):
    """A query with no overlap returns zero across all types."""
    result = SearchService(seeded).search_all("xyznotmatching", limit=30)
    assert result.total == 0


@pytest.mark.unit
def test_semantic_search_falls_back_silently_on_provider_error(seeded):
    """If the embedding provider raises, return [] (don't propagate)."""
    service = SearchService(seeded)
    with patch(
        "app.services.ai_provider.embed_provider.get_embedding_params",
        side_effect=RuntimeError("provider down"),
    ):
        ids = service._semantic_document_ids("anything", k=5)
    assert ids == []


@pytest.mark.unit
def test_semantic_document_search_orders_by_returned_id_list(seeded):
    """When `_semantic_document_ids` returns IDs, `semantic_document_search`
    must return Document objects in that exact order — vector ranking matters."""
    service = SearchService(seeded)
    docs = service.db.query(Document).all()
    if len(docs) < 2:
        # Add a second doc so we have something to order
        case = service.db.query(Case).first()
        batch = service.db.query(IngestBatch).first()
        d2 = Document(
            title="Second", content="other", ingest_batch_id=batch.id, case_id=case.id
        )
        service.db.add(d2)
        service.db.commit()
        docs = service.db.query(Document).all()
    id_a, id_b = docs[0].id, docs[1].id

    with patch.object(service, "_semantic_document_ids", return_value=[id_b, id_a]):
        result = service.semantic_document_search("query", limit=5)

    assert [d.id for d in result] == [id_b, id_a]


@pytest.mark.unit
def test_search_all_merges_semantic_only_document_hit(seeded):
    """A document with no keyword overlap but a semantic hit still surfaces
    in search_all's documents — the merge CLAUDE.md describes but that
    previously had no caller."""
    service = SearchService(seeded)
    case = service.db.query(Case).first()
    batch = service.db.query(IngestBatch).first()
    semantic_only = Document(
        title="Completely unrelated title",
        content="No keyword overlap with the query at all",
        ingest_batch_id=batch.id,
        case_id=case.id,
    )
    service.db.add(semantic_only)
    service.db.commit()

    with patch.object(
        service, "_semantic_document_ids", return_value=[semantic_only.id]
    ):
        result = service.search_all("unterhalt", limit=30)

    doc_ids = {d.id for d in result.documents}
    assert semantic_only.id in doc_ids
    # The keyword hit from the `seeded` fixture is still present.
    assert any("Unterhalts" in d.title for d in result.documents)


@pytest.mark.unit
def test_search_all_documents_dedup_keyword_and_semantic_hit(seeded):
    """A document matching both keyword and semantic search appears once."""
    service = SearchService(seeded)
    keyword_doc = (
        service.db.query(Document).filter(Document.title.ilike("%Unterhalts%")).first()
    )

    with patch.object(service, "_semantic_document_ids", return_value=[keyword_doc.id]):
        result = service.search_all("unterhalt", limit=30)

    doc_ids = [d.id for d in result.documents]
    assert doc_ids.count(keyword_doc.id) == 1


@pytest.mark.unit
def test_search_all_falls_back_to_keyword_only_when_semantic_empty(seeded):
    """When the embedding layer has nothing to offer (provider down, dim
    mismatch, etc. — `nearest_document_ids` already swallows those into an
    empty list), search_all still surfaces keyword hits unaffected."""
    service = SearchService(seeded)
    with patch.object(service, "_semantic_document_ids", return_value=[]):
        result = service.search_all("unterhalt", limit=30)
    assert any("Unterhalts" in d.title for d in result.documents)

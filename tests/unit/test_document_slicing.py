"""Splitting a PDF that is already a document (an email attachment)."""

import pytest

from app.models.database import (
    BatchSubGroup,
    Document,
    DocumentPin,
    DocumentRelationship,
    IngestBatch,
    User,
)
from app.models.enums import (
    DocumentRole,
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    RelationshipType,
)
from app.schemas.slicing import SliceCut
from app.services import slicing_service
from app.services.ingestion import page_text
from app.services.pipeline_status import initialize

PAGES = 4


def _pdf(path, pages=PAGES):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(200, 300)
    pdf.save(str(path))
    pdf.close()


def _chunks(pages=PAGES, failed=()):
    return [
        {
            "text": f"Dies ist der Text der Seite {n}.",
            "meta": {"page": n, "source": "chandra-ocr-2", "failed": n in failed},
        }
        for n in range(1, pages + 1)
    ]


@pytest.fixture
def env(db_session, tmp_path):
    """An own-mail body (d-112) with a 4-page PDF attachment (d-113), already read."""
    owner = db_session.query(User).filter_by(email="admin@localhost").one()
    batch = IngestBatch(
        owner_id=owner.id,
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PROCESSING,
    )
    db_session.add(batch)
    db_session.flush()
    body = Document(
        title="Anweisung",
        owner_id=owner.id,
        case_id="_TRIAGE",
        ingest_batch_id=batch.id,
        role=DocumentRole.COVER_LETTER,
        originator_type=OriginatorType.OWN,
        original_filename=f"email_body_{batch.id}.txt",
    )
    db_session.add(body)
    db_session.flush()
    pdf = tmp_path / f"{batch.id}_packet.pdf"
    _pdf(pdf)
    source = Document(
        title="Begleitschreiben",
        owner_id=owner.id,
        case_id="_TRIAGE",
        ingest_batch_id=batch.id,
        parent_id=body.id,
        role=DocumentRole.ENCLOSURE,
        file_path=str(pdf),
        original_filename="packet.pdf",
        page_count=PAGES,
        meta={
            "chunks": _chunks(),
            "ocr_unverified_pages": [3],
            "extractor": "chandra-ocr-2",
            "slicing": {"status": "ready", "page_count": PAGES},
        },
    )
    db_session.add(source)
    db_session.flush()
    for d in (body, source):
        initialize(d, batched=True, db=db_session)
    db_session.commit()
    return db_session, batch, body, source


CUTS = [SliceCut(page=2, kind="attachment"), SliceCut(page=3, kind="letter")]


@pytest.mark.unit
def test_parts_take_the_sources_place_in_the_family(env):
    db, batch, body, source = env
    source_path = source.file_path
    source_id = source.id

    ids = slicing_service.confirm_document_slices(db, source, CUTS)

    assert len(ids) == 3
    assert db.get(Document, source_id) is None
    import os

    assert not os.path.exists(source_path)
    first, second, third = (db.get(Document, i) for i in ids)
    # Letters stand under the source's parent; the attachment under its lead.
    assert first.parent_id == body.id and third.parent_id == body.id
    assert second.parent_id == first.id
    assert [first.role, second.role, third.role] == [DocumentRole.ENCLOSURE] * 3
    assert all(d.ingest_batch_id == batch.id for d in (first, second, third))
    edges = {
        (e.from_document_id, e.to_document_id)
        for e in db.query(DocumentRelationship)
        .filter(DocumentRelationship.relationship_type == RelationshipType.ENCLOSES)
        .all()
    }
    assert {(body.id, first.id), (first.id, second.id), (body.id, third.id)} <= edges
    # One group holds the mail and its parts, mail first: batch analysis then
    # leaves the roles alone.
    db.refresh(body)
    groups = db.query(BatchSubGroup).filter_by(batch_id=batch.id).all()
    assert len(groups) == 1
    members = [body, first, second, third]
    assert {d.sub_group_id for d in members} == {groups[0].id}
    assert [d.sub_group_sort_order for d in members] == [0, 1, 2, 3]
    assert [d.page_count for d in (first, second, third)] == [2, 1, 1]


@pytest.mark.unit
def test_parts_carry_the_sources_reading_of_their_pages(env):
    db, _batch, _body, source = env

    ids = slicing_service.confirm_document_slices(db, source, CUTS)

    first, second, _third = (db.get(Document, i) for i in ids)
    prefilled = first.meta["prefilled_extraction"]
    assert prefilled["content"] == (
        "--- PAGE 1 ---\n\nDies ist der Text der Seite 1.\n\n--- PAGE 2 ---\n\nDies ist der Text der Seite 2."
    )
    assert [c["meta"]["page"] for c in prefilled["chunks"]] == [1, 2]
    # The source's page 3 was unverified; in its part it is page 1.
    assert second.meta["prefilled_extraction"]["metadata"]["ocr_unverified_pages"] == [
        1
    ]


@pytest.mark.unit
def test_a_part_without_a_usable_reading_is_extracted_from_scratch(env):
    db, _batch, _body, source = env
    source.meta = {**source.meta, "chunks": _chunks(failed=(2,))}
    db.commit()

    ids = slicing_service.confirm_document_slices(db, source, CUTS)

    first, second, _third = (db.get(Document, i) for i in ids)
    assert "prefilled_extraction" not in first.meta  # page 2 failed
    assert "prefilled_extraction" in second.meta


@pytest.mark.unit
def test_a_document_with_your_own_work_is_not_replaced(env):
    db, _batch, _body, source = env
    db.add(
        DocumentPin(
            document_id=source.id,
            user_id=source.owner_id,
            passage_id="abc123def456",
            note="x",
        )
    )
    db.commit()
    before = db.query(Document).count()

    with pytest.raises(slicing_service.SourceHasUserData) as err:
        slicing_service.confirm_document_slices(db, source, CUTS)

    assert err.value.reasons == ["pins"]
    assert db.query(Document).count() == before
    assert db.get(Document, source.id) is not None


@pytest.mark.unit
def test_the_discarded_pages_are_left_out(env):
    db, _batch, _body, source = env

    ids = slicing_service.confirm_document_slices(db, source, CUTS, discard=[4])

    # Page 4 was the whole third part, so it vanishes with its letter opener
    # carried to nothing; two parts remain.
    assert len(ids) == 2


@pytest.mark.unit
def test_extraction_for_pages_needs_every_page_read(env):
    _db, _batch, _body, source = env

    assert page_text.extraction_for_pages(source, [1, 2]) is not None
    source.meta = {**source.meta, "chunks": _chunks(failed=(2,))}
    assert page_text.extraction_for_pages(source, [1, 2]) is None
    assert page_text.page_texts(source).keys() == {1, 3, 4}


@pytest.mark.unit
def test_a_prefilled_part_is_not_ocrd_again(env, monkeypatch):
    from app.services.ingestion import service

    db, _batch, _body, source = env
    ids = slicing_service.confirm_document_slices(db, source, CUTS)
    part = db.get(Document, ids[0])

    def no_ocr(*_a, **_kw):
        raise AssertionError("the part must not be extracted again")

    monkeypatch.setattr(service, "convert_file", no_ocr)
    service.process_uploaded_document(part, db)

    assert "Text der Seite 2" in part.content
    assert "prefilled_extraction" not in part.meta
    assert part.meta["reused_from_document"] == source.id


@pytest.mark.unit
def test_prepare_document_reuses_the_stored_page_text(env, monkeypatch):
    from app.services.ingestion import slicer

    db, _batch, _body, source = env
    source.meta = {**source.meta, "slicing": {"status": "preparing"}}
    db.commit()
    judged: list = []

    async def judge(candidates, model):
        judged.extend(candidates)
        return {
            c.page: {"is_new_document": True, "confidence": "high"} for c in candidates
        }

    async def no_outline(prompt, n_parts, model):
        return {}

    def no_ocr(_img):
        raise AssertionError("every page was already read")

    monkeypatch.setattr(slicer, "_ai_cut_judgments", judge)
    monkeypatch.setattr(slicer, "_ai_outline", no_outline)
    monkeypatch.setattr(slicer, "_ocr_page_text", no_ocr)

    slicer.prepare_document(source.id)

    db.refresh(source)
    slicing = source.meta["slicing"]
    assert slicing["status"] == "ready"
    assert [p["text_head"] for p in slicing["pages"]][
        1
    ] == "Dies ist der Text der Seite 2."
    # Thumbnails of a document live in a folder of its own: the email's folder
    # is shared with other files.
    assert slicing["pages"][0]["thumbnail_path"].endswith(
        "packet_thumbs/page_1.png".replace("packet", f"{source.ingest_batch_id}_packet")
    )
    assert len(judged) == PAGES - 1


# --- API --------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    dispatched: list = []
    monkeypatch.setattr(
        "app.tasks.dispatch.dispatch_task",
        lambda task, *args, **kw: dispatched.append((task, args)),
    )
    test_client = TestClient(app, raise_server_exceptions=False)
    test_client.dispatched = dispatched  # type: ignore[attr-defined]
    return test_client


@pytest.mark.unit
def test_starting_a_split_queues_the_proposal(env, client):
    db, _batch, _body, source = env
    source.meta = {"chunks": source.meta["chunks"]}  # nothing started yet
    db.commit()

    resp = client.post(f"/api/v1/slicing/documents/{source.id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["document_id"] == source.id and body["batch_id"] is None
    assert body["status"] == "preparing"
    assert [args for _task, args in client.dispatched] == [(source.id,)]
    # Asking again while it prepares does not queue a second proposal.
    client.post(f"/api/v1/slicing/documents/{source.id}")
    assert len(client.dispatched) == 1


@pytest.mark.unit
def test_a_single_page_pdf_cannot_be_split(env, client):
    db, _batch, _body, source = env
    source.page_count = 1
    db.commit()

    resp = client.post(f"/api/v1/slicing/documents/{source.id}")

    assert resp.status_code == 409 and resp.json()["code"] == "unsliceable"


@pytest.mark.unit
def test_confirm_waits_for_the_proposal(env, client):
    db, _batch, _body, source = env
    source.meta = {**source.meta, "slicing": {"status": "preparing"}}
    db.commit()

    resp = client.post(
        f"/api/v1/slicing/documents/{source.id}/confirm", json={"cuts": []}
    )

    assert resp.status_code == 409 and resp.json()["code"] == "not_ready"


@pytest.mark.unit
def test_confirm_replaces_the_document_and_queues_its_parts(env, client):
    db, batch, _body, source = env
    source_id = source.id

    resp = client.post(
        f"/api/v1/slicing/documents/{source_id}/confirm",
        json={"cuts": [{"page": 2, "kind": "letter"}]},
    )

    assert resp.status_code == 200
    ids = resp.json()["document_ids"]
    assert len(ids) == 2
    db.expire_all()
    assert db.get(Document, source_id) is None
    assert sorted(args[0] for _task, args in client.dispatched) == sorted(ids)
    db.refresh(batch)
    assert batch.analysis_queued_at is None and batch.metadata_phase_queued_at is None


@pytest.mark.unit
def test_confirm_names_the_work_that_would_be_lost(env, client):
    db, _batch, _body, source = env
    db.add(
        DocumentPin(
            document_id=source.id,
            user_id=source.owner_id,
            passage_id="abc123def456",
        )
    )
    db.commit()

    resp = client.post(
        f"/api/v1/slicing/documents/{source.id}/confirm", json={"cuts": []}
    )

    assert resp.status_code == 409
    assert resp.json()["code"] == "has_user_data"
    assert "pins" in resp.json()["detail"]
    assert client.dispatched == []


# --- Safety of replacing the source -----------------------------------------


def _action_item(db, source, **kw):
    from datetime import UTC, datetime

    from app.models.database import ActionItem, Case
    from app.models.enums import ActionItemType

    if not db.get(Case, "AI-1"):
        db.add(Case(id="AI-1", title="AI-1", owner_id=source.owner_id))
        db.flush()
    item = ActionItem(
        case_id="AI-1",
        source_document_id=source.id,
        title=kw.pop("title", "Stellungnahme"),
        due_date=datetime(2026, 9, 15, tzinfo=UTC),
        action_type=ActionItemType.DEADLINE,
        **kw,
    )
    db.add(item)
    db.commit()
    return item


@pytest.mark.unit
def test_the_sources_ai_action_items_go_with_it(env):
    from app.models.database import ActionItem

    db, _batch, _body, source = env
    _action_item(db, source)

    slicing_service.confirm_document_slices(db, source, CUTS)

    # Not orphaned: the parts raise the deadline again from their own text.
    assert db.query(ActionItem).count() == 0


@pytest.mark.unit
def test_an_action_item_you_closed_is_not_lost(env):
    from app.models.enums import ActionItemStatus

    db, _batch, _body, source = env
    _action_item(db, source, status=ActionItemStatus.COMPLETED)

    with pytest.raises(slicing_service.SourceHasUserData) as err:
        slicing_service.confirm_document_slices(db, source, CUTS)

    assert err.value.reasons == ["action items"]


@pytest.mark.unit
def test_edits_and_chats_on_the_source_are_named(env):
    from app.models.database import Conversation

    db, _batch, _body, source = env
    source.extraction_confidence = {"title": "user_set", "case_id": "user_set"}
    db.add(
        Conversation(
            scope_type="document", scope_id=str(source.id), user_id=source.owner_id
        )
    )
    db.commit()

    with pytest.raises(slicing_service.SourceHasUserData) as err:
        slicing_service.confirm_document_slices(db, source, CUTS)

    assert err.value.reasons == ["edited title", "chats"]


@pytest.mark.unit
def test_a_document_still_being_processed_is_not_replaced(env):
    from app.models.enums import PipelineStage
    from app.services.pipeline_status import mark_started

    db, _batch, body, source = env
    # A sibling's stage is running: replacing the source would race it.
    mark_started(body.id, PipelineStage.ENRICH, db)

    with pytest.raises(slicing_service.SourceInFlight):
        slicing_service.confirm_document_slices(db, source, CUTS)

    assert db.get(Document, source.id) is not None


@pytest.mark.unit
def test_parts_stand_where_the_source_stood_in_its_group(env):
    db, batch, body, source = env
    owner_id = source.owner_id
    group = BatchSubGroup(batch_id=batch.id, sort_order=0)
    db.add(group)
    db.flush()
    tail = Document(
        title="Anlage",
        owner_id=owner_id,
        case_id="_TRIAGE",
        ingest_batch_id=batch.id,
    )
    db.add(tail)
    db.flush()
    for doc, pos in ((body, 0), (source, 1), (tail, 2)):
        doc.sub_group_id = group.id
        doc.sub_group_sort_order = pos
    db.commit()

    ids = slicing_service.confirm_document_slices(db, source, CUTS)

    db.expire_all()
    ordered = (
        db.query(Document)
        .filter(Document.sub_group_id == group.id)
        .order_by(Document.sub_group_sort_order)
        .all()
    )
    assert [d.id for d in ordered] == [body.id, *ids, tail.id]


@pytest.mark.unit
def test_a_loose_document_without_a_batch_is_split_too(env):
    db, _batch, _body, source = env
    source.ingest_batch_id = None
    source.parent_id = None
    db.commit()

    ids = slicing_service.confirm_document_slices(db, source, CUTS)

    first, second, third = (db.get(Document, i) for i in ids)
    assert first.role == DocumentRole.COVER_LETTER
    assert second.parent_id == first.id
    assert third.role == DocumentRole.STANDALONE
    assert third.sub_group_id is None  # no batch to group in


@pytest.mark.unit
def test_a_failed_delete_leaves_the_source_and_its_pdf_untouched(env, monkeypatch):
    import os

    from app.services.document_service import DocumentService

    db, _batch, _body, source = env
    source_id, source_path = source.id, source.file_path
    before = db.query(Document).count()

    def boom(self, doc_id):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(DocumentService, "delete_document", boom)

    with pytest.raises(slicing_service.SlicingFailed):
        slicing_service.confirm_document_slices(db, source, CUTS)

    db.expire_all()
    kept = db.get(Document, source_id)
    assert kept is not None and kept.file_path == source_path
    assert os.path.exists(source_path)
    assert db.query(Document).count() == before
    assert not [p for p in os.listdir(os.path.dirname(source_path)) if "_part" in p]


@pytest.mark.unit
def test_a_part_never_overwrites_a_sibling_attachment_with_the_same_name(env):
    import os
    from pathlib import Path

    db, _batch, _body, source = env
    sibling = str(
        Path(source.file_path).with_name(f"{Path(source.file_path).stem}_part1.pdf")
    )
    Path(sibling).write_bytes(b"another attachment")

    ids = slicing_service.confirm_document_slices(db, source, CUTS)

    assert Path(sibling).read_bytes() == b"another attachment"
    first = db.get(Document, ids[0])
    assert first.file_path != sibling and os.path.exists(first.file_path)


@pytest.mark.unit
def test_only_the_sources_own_thumbnail_folder_is_removed(env, tmp_path):
    import os

    db, _batch, _body, source = env
    folder = os.path.dirname(source.file_path)
    stem = os.path.splitext(os.path.basename(source.file_path))[0]
    own = os.path.join(folder, f"{stem}_thumbs")
    other = os.path.join(folder, "someone_elses_thumbs")
    for d in (own, other):
        os.makedirs(d)
    # A thumbnail path pointing somewhere else must not steer the cleanup.
    source.meta = {
        **source.meta,
        "slicing": {
            "status": "ready",
            "page_count": PAGES,
            "pages": [{"thumbnail_path": os.path.join(other, "page_1.png")}],
        },
    }
    db.commit()

    slicing_service.confirm_document_slices(db, source, CUTS)

    assert not os.path.exists(own) and os.path.exists(other)

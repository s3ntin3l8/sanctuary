"""/api/v1/documents/*: review view and the actions on it; upload."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    ActionItem,
    Case,
    Document,
    DocumentPipelineStage,
    DocumentRelationship,
    IngestBatch,
    User,
)
from app.models.enums import (
    ActionItemType,
    CaseStatus,
    IngestBatchSourceType,
    IngestBatchStatus,
    PipelineStage,
    PipelineState,
    RelationshipConfidence,
    RelationshipType,
    StageStatus,
)

pytestmark = pytest.mark.integration

client = TestClient(app)


def _admin(db):
    return db.query(User).filter_by(email="admin@localhost").one()


def _doc(db, owner_id, case_id="_TRIAGE", **kw):
    doc = Document(
        title=kw.pop("title", "Klageerwiderung"),
        owner_id=owner_id,
        case_id=case_id,
        pipeline_state=kw.pop("pipeline_state", PipelineState.COMPLETED),
        ai_summary={
            "legal_significance": "rebuts custody claim",
            "required_action": "file counter-statement",
            "financial_impact": "1.3 RVG fee",
        },
        key_passages=[
            {"text": "Der Beklagte bestreitet", "kind": "disputed", "page": 2}
        ],
        extraction_confidence={"sender": "low", "issued_date": "medium"},
        review_reasons=["missing_sender"],
        needs_review=True,
        **kw,
    )
    db.add(doc)
    db.commit()
    return doc


# --- Review view --------------------------------------------------------------


def test_review_view_shape(db_session, sample_case):
    admin = _admin(db_session)
    doc = _doc(db_session, admin.id)
    other = _doc(db_session, admin.id, title="Antragsschrift")
    db_session.add(
        DocumentRelationship(
            from_document_id=doc.id,
            to_document_id=other.id,
            relationship_type=RelationshipType.REPLIES_TO,
            confidence=RelationshipConfidence.AI_DETECTED,
        )
    )
    db_session.add(
        ActionItem(
            case_id="_TRIAGE",
            source_document_id=doc.id,
            title="File counter-statement",
            action_type=ActionItemType.DEADLINE,
            due_date=datetime.now(UTC) + timedelta(days=4),
        )
    )
    db_session.add(
        DocumentPipelineStage(
            document_id=doc.id, stage=PipelineStage.ENRICH, status=StageStatus.COMPLETED
        )
    )
    db_session.commit()

    body = client.get(f"/api/v1/documents/{doc.id}/review").json()
    assert body["title"] == "Klageerwiderung"
    assert body["case"] is None
    assert [b["kind"] for b in body["summary"]["bullets"]] == [
        "legal",
        "action",
        "finance",
    ]
    assert body["summary"]["enrich_status"] == "completed"
    (passage,) = body["key_passages"]
    assert passage["text"].startswith("Der Beklagte") and passage["page"] == 2
    (rel,) = body["relationships"]
    assert (
        rel["doc_id"] == other.id
        and rel["direction"] == "out"
        and rel["rel_type"] == "replies_to"
    )
    (action,) = body["actions"]
    assert action["title"] == "File counter-statement" and action["status"] == "open"
    sender = next(f for f in body["metadata"] if f["field"] == "sender")
    assert sender["confidence"] == "low"
    assert [s["key"] for s in body["pipeline"]["stages"]][:2] == ["extract", "metadata"]
    assert body["claims_status"] == "pending_triage"
    assert [c["id"] for c in body["cases"]] == [sample_case.id]


def test_review_pipeline_reports_ocr_page_failures(db_session):
    admin = _admin(db_session)
    clean = _doc(db_session, admin.id)
    partial = _doc(db_session, admin.id, meta={"page_failures": [3, 1]})

    def failures(doc):
        body = client.get(f"/api/v1/documents/{doc.id}/review").json()
        return body["pipeline"]["ocr_page_failures"]

    assert failures(clean) == []
    assert failures(partial) == [1, 3]


def _chunk(page, words):
    return {
        "text": "",
        "meta": {"page": page, "crosscheck": {"unsupported": words, "ratio": 0.2}},
    }


def test_review_lists_pages_the_ocr_crosscheck_could_not_corroborate(db_session):
    admin = _admin(db_session)
    doc = _doc(
        db_session,
        admin.id,
        meta={
            "ocr_unverified_pages": [3],
            "chunks": [_chunk(1, []), _chunk(2, ["x"]), _chunk(3, ["wohnplatz"])],
        },
    )

    body = client.get(f"/api/v1/documents/{doc.id}/review").json()

    assert body["pipeline"]["ocr_unverified"] == [{"page": 3, "words": ["wohnplatz"]}]


def test_acknowledging_the_ocr_crosscheck_clears_the_flag(db_session):
    admin = _admin(db_session)
    doc = _doc(
        db_session,
        admin.id,
        meta={"ocr_unverified_pages": [3], "chunks": [_chunk(3, ["wohnplatz"])]},
    )

    resp = client.post(f"/api/v1/documents/{doc.id}/ocr-unverified/acknowledge")

    assert resp.status_code == 200
    body = resp.json()
    assert body["pipeline"]["ocr_unverified"] == []
    assert "ocr_unverified" not in body["review_reasons"]
    db_session.refresh(doc)
    assert doc.meta["ocr_unverified_acknowledged"] == [3]


def test_review_says_when_the_ocr_crosscheck_could_not_run(db_session):
    admin = _admin(db_session)
    ran = _doc(db_session, admin.id, meta={"chunks": []})
    skipped = _doc(db_session, admin.id, meta={"ocr_crosscheck_unavailable": True})

    def unavailable(doc):
        body = client.get(f"/api/v1/documents/{doc.id}/review").json()
        return body["pipeline"]["ocr_crosscheck_unavailable"]

    assert unavailable(ran) is False
    assert unavailable(skipped) is True


def test_acknowledging_a_clean_document_writes_nothing(db_session):
    admin = _admin(db_session)
    doc = _doc(db_session, admin.id, meta={"chunks": []})

    assert (
        client.post(
            f"/api/v1/documents/{doc.id}/ocr-unverified/acknowledge"
        ).status_code
        == 200
    )

    db_session.refresh(doc)
    assert "ocr_unverified_acknowledged" not in doc.meta


def test_review_requires_access(auth_enabled, db_session):
    from app.services import auth_service

    admin = _admin(db_session)
    doc = _doc(db_session, admin.id)
    other = auth_service.create_user(
        db_session,
        email="other@example.com",
        password="password123",  # pragma: allowlist secret
    )
    db_session.commit()
    c = TestClient(app)
    c.post(
        "/api/v1/auth/login",
        json={
            "email": other.email,
            "password": "password123",  # pragma: allowlist secret
        },  # pragma: allowlist secret
    )
    assert c.get(f"/api/v1/documents/{doc.id}/review").status_code == 404


# --- Actions -----------------------------------------------------------------


def test_metadata_update_recomputes_review(db_session):
    admin = _admin(db_session)
    doc = _doc(db_session, admin.id)
    body = client.put(
        f"/api/v1/documents/{doc.id}/metadata",
        json={
            "sender": "RA Müller",
            "issued_date": "2026-06-14T00:00:00Z",
            "significance_tier": "critical",
        },
    ).json()
    assert body["sender"] == "RA Müller"
    assert body["significance_tier"] == "critical"
    assert "missing_sender" not in body["review_reasons"]
    assert body["title"] == "Klageerwiderung"  # untouched fields stay


def test_summary_approve_and_reject(db_session):
    admin = _admin(db_session)
    doc = _doc(db_session, admin.id)
    approved = client.post(
        f"/api/v1/documents/{doc.id}/summary", json={"action": "approve"}
    ).json()
    assert approved["approved_at"] is not None
    rejected = client.post(
        f"/api/v1/documents/{doc.id}/summary", json={"action": "reject"}
    ).json()
    assert rejected["bullets"] == [] and rejected["approved_at"] is None


def test_reactions_toggle_and_note(db_session):
    admin = _admin(db_session)
    doc = _doc(db_session, admin.id)
    url = f"/api/v1/documents/{doc.id}/reactions"
    assert [
        r["reaction"] for r in client.post(url, json={"reaction": "lies"}).json()
    ] == ["lies"]
    assert client.post(url, json={"reaction": "lies"}).json() == []
    noted = client.post(
        url, json={"reaction": "needs_proof", "notes": "ask for the Jugendamt file"}
    ).json()
    assert noted[0]["notes"] == "ask for the Jugendamt file"


def test_action_item_status_and_relationship_decisions(db_session, sample_case):
    admin = _admin(db_session)
    doc = _doc(db_session, admin.id)
    other = _doc(db_session, admin.id, title="Other")
    item = ActionItem(
        case_id="_TRIAGE",
        source_document_id=doc.id,
        title="x",
        action_type=ActionItemType.DEADLINE,
        due_date=datetime.now(UTC),
    )
    rel = DocumentRelationship(
        from_document_id=doc.id,
        to_document_id=other.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.AI_DETECTED,
    )
    rel2 = DocumentRelationship(
        from_document_id=other.id,
        to_document_id=doc.id,
        relationship_type=RelationshipType.REFERENCES,
        confidence=RelationshipConfidence.AI_DETECTED,
    )
    db_session.add_all([item, rel, rel2])
    db_session.commit()

    assert (
        client.patch(
            f"/api/v1/action-items/{item.id}", json={"status": "completed"}
        ).json()["status"]
        == "completed"
    )
    assert client.post(f"/api/v1/relationships/{rel.id}/confirm").status_code == 204
    db_session.expire_all()
    assert (
        db_session.get(DocumentRelationship, rel.id).confidence
        == RelationshipConfidence.USER_CONFIRMED
    )
    assert client.delete(f"/api/v1/relationships/{rel2.id}").status_code == 204
    assert db_session.query(DocumentRelationship).filter_by(id=rel2.id).first() is None
    assert client.delete("/api/v1/relationships/999999").status_code == 404


def test_pipeline_retry_rules(db_session):
    admin = _admin(db_session)
    doc = _doc(db_session, admin.id, pipeline_state=PipelineState.FAILED)
    db_session.add_all(
        [
            DocumentPipelineStage(
                document_id=doc.id,
                stage=PipelineStage.EXTRACT,
                status=StageStatus.COMPLETED,
            ),
            DocumentPipelineStage(
                document_id=doc.id,
                stage=PipelineStage.METADATA,
                status=StageStatus.RUNNING,
            ),
            DocumentPipelineStage(
                document_id=doc.id,
                stage=PipelineStage.ENRICH,
                status=StageStatus.FAILED,
            ),
        ]
    )
    db_session.commit()
    blocked = client.post(f"/api/v1/documents/{doc.id}/pipeline/enrich/retry")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "upstream_running"
    running = client.post(f"/api/v1/documents/{doc.id}/pipeline/metadata/retry")
    assert running.status_code == 409 and running.json()["code"] == "in_flight"
    assert (
        client.post(f"/api/v1/documents/{doc.id}/pipeline/retry-all").status_code == 409
    )
    assert (
        client.post(f"/api/v1/documents/{doc.id}/pipeline/nope/retry").status_code
        == 422
    )

    db_session.query(DocumentPipelineStage).filter_by(
        document_id=doc.id, stage=PipelineStage.METADATA
    ).update({"status": StageStatus.COMPLETED})
    db_session.commit()
    with patch("app.api.v1.documents.dispatch_pipeline_retry") as dispatch:
        view = client.post(f"/api/v1/documents/{doc.id}/pipeline/enrich/retry").json()
    assert dispatch.call_args.args[2] == PipelineStage.ENRICH
    assert (
        next(s for s in view["stages"] if s["key"] == "enrich")["status"] == "pending"
    )


def test_document_status_labels(db_session):
    admin = _admin(db_session)
    doc = _doc(db_session, admin.id, pipeline_state=PipelineState.FAILED)
    db_session.add(
        DocumentPipelineStage(
            document_id=doc.id,
            stage=PipelineStage.EXTRACT,
            status=StageStatus.FAILED,
            error="boom",
        )
    )
    db_session.commit()
    body = client.get(f"/api/v1/documents/{doc.id}/status").json()
    assert body == {
        "id": doc.id,
        "state": "failed",
        "label": "extract failed",
        "error": "boom",
    }


def test_draft_case_confirm_and_reject(db_session):
    admin = _admin(db_session)
    draft = Case(
        id="DRAFT-1",
        title="AI draft",
        status=CaseStatus.INTAKE,
        is_draft=True,
        owner_id=admin.id,
    )
    db_session.add(draft)
    db_session.commit()
    doc = _doc(db_session, admin.id, case_id="DRAFT-1")
    assert (
        client.post("/api/v1/cases/DRAFT-1/confirm-draft").json()["is_draft"] is False
    )
    assert client.post("/api/v1/cases/DRAFT-1/reject-draft").status_code == 400

    draft2 = Case(
        id="DRAFT-2",
        title="AI draft 2",
        status=CaseStatus.INTAKE,
        is_draft=True,
        owner_id=admin.id,
    )
    db_session.add(draft2)
    db_session.commit()
    doc2 = _doc(db_session, admin.id, case_id="DRAFT-2")
    assert client.post("/api/v1/cases/DRAFT-2/reject-draft").status_code == 204
    db_session.expire_all()
    assert db_session.get(Case, "DRAFT-2") is None
    assert db_session.get(Document, doc2.id).case_id == "_TRIAGE"
    assert db_session.get(Document, doc.id).case_id == "DRAFT-1"


# --- Upload ------------------------------------------------------------------


def test_upload_queues_files_and_reports_duplicates(db_session):
    with patch("app.tasks.dispatch.dispatch_task"):
        first = client.post(
            "/api/v1/upload",
            files=[("files", ("brief.txt", b"hello world", "text/plain"))],
        ).json()
        assert first["queued"] == 1 and first["results"][0]["status"] == "queued"
        assert first["batch_id"] is not None
        dup = client.post(
            "/api/v1/upload",
            files=[("files", ("brief.txt", b"hello world", "text/plain"))],
        ).json()
    assert dup["results"][0]["status"] == "duplicate"
    assert dup["batch_id"] is None  # empty batch is removed again


def test_upload_rejects_unknown_case_and_empty_selection(db_session):
    resp = client.post(
        "/api/v1/upload",
        files=[("files", ("brief.txt", b"x", "text/plain"))],
        data={"case_id": "NOPE"},
    )
    assert resp.status_code == 404
    assert (
        client.post(
            "/api/v1/upload", files=[("files", ("", b"", "text/plain"))]
        ).status_code
        == 422
    )


def _pdf_bytes(pages: int, tag: str) -> bytes:
    import io

    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    for i in range(pages):
        pdf.new_page(200 + i + len(tag), 300)  # tag length => distinct hash
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def _scan_dirs(monkeypatch, tmp_path):
    from app.services.ingestion import scan_folder

    monkeypatch.setattr(scan_folder, "SCAN_PROCESSING_DIR", tmp_path / "processing")
    monkeypatch.setattr(scan_folder, "SCAN_PROCESSED_DIR", tmp_path / "processed")
    monkeypatch.setattr(scan_folder, "SCAN_FAILED_DIR", tmp_path / "failed")
    return scan_folder


def test_upload_split_scans_queues_multipage_for_slicing(
    db_session, monkeypatch, tmp_path
):
    from app.models.database import Document, IngestBatch
    from app.models.enums import IngestBatchSourceType, IngestBatchStatus

    _scan_dirs(monkeypatch, tmp_path)
    dispatched = []
    monkeypatch.setattr(
        "app.services.ingestion.batch_orchestrator.dispatch_task",
        lambda task, *a, **k: dispatched.append(task),
    )
    data = _pdf_bytes(3, "multi")
    body = client.post(
        "/api/v1/upload",
        files=[("files", ("stack.pdf", data, "application/pdf"))],
        data={"split_scans": "true"},
    ).json()
    result = body["results"][0]
    assert result["status"] == "queued" and result["slicing"] is True
    batch = db_session.get(IngestBatch, result["batch_id"])
    assert batch.status == IngestBatchStatus.AWAITING_SLICING
    assert batch.source_type == IngestBatchSourceType.SCAN
    assert batch.subject == "stack.pdf"
    assert len(list((tmp_path / "processed").glob("*/*/original.pdf"))) == 1
    assert any("prepare_slicing" in str(t) for t in dispatched)
    assert db_session.query(Document).filter_by(ingest_batch_id=batch.id).count() == 0

    dup = client.post(
        "/api/v1/upload",
        files=[("files", ("stack.pdf", data, "application/pdf"))],
        data={"split_scans": "true"},
    ).json()
    assert dup["results"][0]["status"] == "duplicate"
    assert len(list((tmp_path / "processed").glob("*/*"))) == 1


def test_upload_split_scans_single_page_becomes_a_document(
    db_session, monkeypatch, tmp_path
):
    from app.models.database import Document

    _scan_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.services.ingestion.batch_orchestrator.dispatch_task", lambda *a, **k: None
    )
    result = client.post(
        "/api/v1/upload",
        files=[("files", ("letter.pdf", _pdf_bytes(1, "single"), "application/pdf"))],
        data={"split_scans": "true"},
    ).json()["results"][0]
    assert result["status"] == "queued" and result["slicing"] is False
    doc = db_session.get(Document, result["doc_id"])
    assert doc.ingest_batch_id == result["batch_id"] and doc.title == "letter.pdf"


def test_upload_split_scans_rejects_case_target_and_non_pdf(
    db_session, sample_case, monkeypatch, tmp_path
):
    _scan_dirs(monkeypatch, tmp_path)
    resp = client.post(
        "/api/v1/upload",
        files=[("files", ("a.pdf", b"%PDF-1.4", "application/pdf"))],
        data={"split_scans": "true", "case_id": sample_case.id},
    )
    assert resp.status_code == 422 and resp.json()["code"] == "split_needs_triage"

    fake = client.post(
        "/api/v1/upload",
        files=[("files", ("fake.pdf", b"not a pdf at all", "application/pdf"))],
        data={"split_scans": "true"},
    ).json()
    assert fake["results"][0]["status"] == "error"
    assert not list((tmp_path / "processing").glob("*"))


def test_upload_filename_is_echoed_safely(db_session):
    name = "<img src=x onerror=alert(1)>.txt"
    with patch("app.tasks.dispatch.dispatch_task"):
        body = client.post(
            "/api/v1/upload", files=[("files", (name, b"xss", "text/plain"))]
        ).json()
    assert body["results"][0]["filename"] == name  # JSON, never interpolated into HTML


def test_upload_target_requires_case_access(db_session, sample_case):
    body = client.get(
        "/api/v1/upload/target", params={"case_id": sample_case.id}
    ).json()
    assert body["case_title"] == sample_case.title
    assert (
        client.get("/api/v1/upload/target", params={"case_id": "NOPE"}).status_code
        == 404
    )


@pytest.mark.integration
def test_retry_refused_while_a_stage_is_retrying_and_retry_all_keeps_skipped(
    db_session,
):
    admin = _admin(db_session)
    doc = _doc(db_session, admin.id, pipeline_state=PipelineState.FAILED)
    db_session.add_all(
        [
            DocumentPipelineStage(
                document_id=doc.id,
                stage=PipelineStage.EXTRACT,
                status=StageStatus.COMPLETED,
            ),
            DocumentPipelineStage(
                document_id=doc.id,
                stage=PipelineStage.METADATA,
                status=StageStatus.RETRYING,
            ),
            DocumentPipelineStage(
                document_id=doc.id,
                stage=PipelineStage.BATCH_ANALYSIS,
                status=StageStatus.SKIPPED,
                reason="manual upload",
            ),
            DocumentPipelineStage(
                document_id=doc.id,
                stage=PipelineStage.ENRICH,
                status=StageStatus.FAILED,
            ),
        ]
    )
    db_session.commit()
    retrying = client.post(f"/api/v1/documents/{doc.id}/pipeline/metadata/retry")
    assert retrying.status_code == 409 and retrying.json()["code"] == "in_flight"
    assert (
        client.post(f"/api/v1/documents/{doc.id}/pipeline/retry-all").status_code == 409
    )

    db_session.query(DocumentPipelineStage).filter_by(
        document_id=doc.id, stage=PipelineStage.METADATA
    ).update({"status": StageStatus.COMPLETED})
    db_session.commit()
    with patch("app.api.v1.documents.dispatch_pipeline_retry"):
        view = client.post(f"/api/v1/documents/{doc.id}/pipeline/retry-all").json()
    by_key = {s["key"]: s["status"] for s in view["stages"]}
    assert by_key["batch_analysis"] == "skipped"
    assert by_key["enrich"] == "pending"


def test_confirming_an_email_header_relationship_is_a_noop(db_session, sample_case):
    admin = _admin(db_session)
    doc = _doc(db_session, admin.id)
    other = _doc(db_session, admin.id, title="Other")
    rel = DocumentRelationship(
        from_document_id=doc.id,
        to_document_id=other.id,
        relationship_type=RelationshipType.REFERENCES,
        confidence=RelationshipConfidence.EMAIL_HEADER,
    )
    db_session.add(rel)
    db_session.commit()

    assert client.post(f"/api/v1/relationships/{rel.id}/confirm").status_code == 204
    db_session.expire_all()
    assert (
        db_session.get(DocumentRelationship, rel.id).confidence
        == RelationshipConfidence.EMAIL_HEADER
    )
    # Rejecting is still allowed.
    assert client.delete(f"/api/v1/relationships/{rel.id}").status_code == 204


def test_rejecting_a_relationship_remembers_it(db_session, sample_case):
    from app.models.database import RejectedRelationship
    from app.repositories.document_relationship import insert_edge_if_absent

    admin = _admin(db_session)
    doc = _doc(db_session, admin.id)
    other = _doc(db_session, admin.id, title="Other")
    rel = DocumentRelationship(
        from_document_id=doc.id,
        to_document_id=other.id,
        relationship_type=RelationshipType.REPLIES_TO,
        confidence=RelationshipConfidence.AI_DETECTED,
    )
    db_session.add(rel)
    db_session.commit()

    assert client.delete(f"/api/v1/relationships/{rel.id}").status_code == 204
    db_session.expire_all()
    assert db_session.query(RejectedRelationship).count() == 1
    assert not insert_edge_if_absent(
        db_session,
        from_document_id=doc.id,
        to_document_id=other.id,
        relationship_type=RelationshipType.REPLIES_TO,
    )


def _batch_with_stale_barriers(db, doc):
    batch = IngestBatch(
        source_type=IngestBatchSourceType.EMAIL,
        received_at=datetime.now(UTC),
        status=IngestBatchStatus.PENDING,
        metadata_phase_queued_at=datetime.now(UTC),
        analysis_queued_at=datetime.now(UTC),
    )
    db.add(batch)
    db.flush()
    doc.ingest_batch_id = batch.id
    db.add_all(
        DocumentPipelineStage(
            document_id=doc.id, stage=stage, status=StageStatus.COMPLETED
        )
        for stage in (PipelineStage.EXTRACT, PipelineStage.METADATA)
    )
    db.commit()
    return batch


def test_single_doc_extract_retries_rearm_the_batch_barriers(db_session):
    admin = _admin(db_session)
    doc = _doc(db_session, admin.id)
    batch = _batch_with_stale_barriers(db_session, doc)

    with patch("app.api.v1.documents.dispatch_pipeline_retry"):
        for url in (
            f"/api/v1/documents/{doc.id}/pipeline/extract/retry",
            f"/api/v1/documents/{doc.id}/pipeline/retry-all",
        ):
            batch.metadata_phase_queued_at = batch.analysis_queued_at = datetime.now(
                UTC
            )
            db_session.commit()
            assert client.post(url).status_code == 200
            db_session.refresh(batch)
            assert batch.metadata_phase_queued_at is None, url
            assert batch.analysis_queued_at is None, url
            db_session.query(DocumentPipelineStage).filter_by(
                document_id=doc.id
            ).update({"status": StageStatus.COMPLETED})
            db_session.commit()


def test_non_extract_stage_retry_leaves_batch_barriers_alone(db_session):
    admin = _admin(db_session)
    doc = _doc(db_session, admin.id)
    batch = _batch_with_stale_barriers(db_session, doc)

    with patch("app.api.v1.documents.dispatch_pipeline_retry"):
        r = client.post(f"/api/v1/documents/{doc.id}/pipeline/enrich/retry")
    assert r.status_code == 200
    db_session.refresh(batch)
    assert batch.metadata_phase_queued_at is not None
    assert batch.analysis_queued_at is not None

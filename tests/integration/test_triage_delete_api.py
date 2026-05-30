from app.models.database import Document, IngestBatch
from app.models.enums import IngestBatchSourceType, IngestBatchStatus, PipelineState


def _make_triage_doc(db_session, batch=None, *, title="Doc"):
    doc = Document(
        title=title,
        ingest_batch_id=batch.id if batch else None,
        pipeline_state=PipelineState.PENDING,
        significance_tier="significant",
    )
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)
    return doc


def test_delete_endpoint_returns_oob_swap(app_client, db_session):
    # Create a second bundle so triage isn't empty after deletion
    other_batch = IngestBatch(
        subject="Other Batch", source_type=IngestBatchSourceType.EMAIL
    )
    db_session.add(other_batch)
    db_session.commit()
    _make_triage_doc(db_session, other_batch, title="Other Doc")

    batch = IngestBatch(
        subject="Delete Target", source_type=IngestBatchSourceType.EMAIL
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)
    doc = _make_triage_doc(db_session, batch, title="Target Doc")
    doc_id = doc.id
    batch_id = batch.id

    response = app_client.post(f"/triage/delete?batch_id={batch_id}")

    assert response.status_code == 200
    assert f'id="triage-row-batch-{batch_id}"' in response.text
    assert 'hx-swap-oob="delete"' in response.text

    # Route used a different session — drop our identity-map cache.
    db_session.expire_all()
    assert db_session.get(IngestBatch, batch_id) is None
    assert db_session.get(Document, doc_id) is None
    # Other bundle untouched
    assert db_session.get(IngestBatch, other_batch.id) is not None


def test_delete_last_bundle_returns_empty_state(app_client, db_session):
    batch = IngestBatch(subject="Only One", source_type=IngestBatchSourceType.EMAIL)
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)
    _make_triage_doc(db_session, batch, title="Sole Doc")
    batch_id = batch.id

    response = app_client.post(f"/triage/delete?batch_id={batch_id}")

    assert response.status_code == 200
    # Empty state re-renders the feed partial
    assert 'id="triage-feed"' in response.text
    # Per-row OOB delete must accompany the empty feed so the deleted row's
    # polling children (`_pipeline_aggregate.html` polls /triage/bundle/{id}/pipeline
    # every 4s) stop firing. Without this, the row stays in the DOM and 404s
    # the server until the user reloads.
    assert f'id="triage-row-batch-{batch_id}"' in response.text
    assert 'hx-swap-oob="delete"' in response.text
    # The feed itself must declare hx-swap-oob="true" so HTMX swaps it
    # regardless of the client's hx-swap setting (request uses hx-swap="none").
    assert 'hx-swap-oob="true"' in response.text


def test_delete_404_unknown_batch(app_client):
    response = app_client.post("/triage/delete?batch_id=999999")
    assert response.status_code == 404


def test_delete_409_processing_batch(app_client, db_session):
    batch = IngestBatch(
        subject="Mid-flight",
        source_type=IngestBatchSourceType.EMAIL,
        status=IngestBatchStatus.PROCESSING,
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)
    _make_triage_doc(db_session, batch, title="Locked Doc")

    response = app_client.post(f"/triage/delete?batch_id={batch.id}")
    assert response.status_code == 409
    # Batch still present
    assert db_session.get(IngestBatch, batch.id) is not None


def test_delete_parent_with_children_and_fk_refs(app_client, db_session, sample_user):
    """Regression: a sliced bundle (parent cover-letter + N enclosure children)
    with UserReaction / DocumentPin / DocumentRelationship rows hanging off the
    children must delete cleanly. Previously failed because Document.children
    has cascade="all, delete-orphan" — deleting the parent first triggered an
    ORM cascade DELETE on the children before our manual FK cleanup ran for
    them, hitting `FOREIGN KEY constraint failed` on documents.id.
    """
    from app.models.database import DocumentPin, DocumentRelationship, UserReaction
    from app.models.enums import RelationshipType, UserReactionType

    batch = IngestBatch(
        subject="Sliced Bundle", source_type=IngestBatchSourceType.EMAIL
    )
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)

    parent = _make_triage_doc(db_session, batch, title="Cover Letter")
    children = [
        _make_triage_doc(db_session, batch, title=f"Enclosure {i}") for i in range(4)
    ]
    for child in children:
        child.parent_id = parent.id
    db_session.commit()

    # Hang FK references off each child — the rows that previously caused the
    # FK guard to fire when the parent's cascade tried to drop the children.
    for child in children:
        db_session.add(
            UserReaction(
                document_id=child.id,
                reaction=UserReactionType.LIES,
                user_id=sample_user.id,
            )
        )
        db_session.add(
            DocumentPin(
                document_id=child.id, passage_id="p1", note="x", user_id=sample_user.id
            )
        )
    db_session.add(
        DocumentRelationship(
            from_document_id=children[0].id,
            to_document_id=children[1].id,
            relationship_type=RelationshipType.REFERENCES,
        )
    )
    db_session.commit()

    # Snapshot IDs before the delete so we don't reload stale ORM state.
    batch_id = batch.id
    parent_id = parent.id
    child_ids = [c.id for c in children]

    response = app_client.post(f"/triage/delete?batch_id={batch_id}")

    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert db_session.get(IngestBatch, batch_id) is None
    assert db_session.get(Document, parent_id) is None
    for cid in child_ids:
        assert db_session.get(Document, cid) is None


def test_delete_loose_doc_via_doc_id(app_client, db_session):
    # Keep triage non-empty
    other_batch = IngestBatch(subject="Other", source_type=IngestBatchSourceType.EMAIL)
    db_session.add(other_batch)
    db_session.commit()
    _make_triage_doc(db_session, other_batch, title="Other")

    doc = _make_triage_doc(db_session, batch=None, title="Loose Doc")
    doc_id = doc.id

    response = app_client.post(f"/triage/delete?doc_id={doc_id}")
    assert response.status_code == 200
    assert f'id="triage-row-loose-{doc_id}"' in response.text
    assert 'hx-swap-oob="delete"' in response.text
    db_session.expire_all()
    assert db_session.get(Document, doc_id) is None

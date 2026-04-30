"""Integration tests for the redesigned triage page.

Locks contracts that distinguish the redesigned layout from the prior split-pane
inbox: row-per-bundle IDs, status-driven stripe class, filter chip taxonomy,
inline expand body, and drawer mount markup.
"""

import pytest

from app.models.database import Document, IngestBatch
from app.models.enums import (
    IngestBatchSourceType,
    IngestBatchStatus,
    PipelineState,
)


# Lazy module-level TestClient — see test_triage.py for rationale.
class _LazyTestClient:
    _real = None

    def __getattr__(self, attr):
        if _LazyTestClient._real is None:
            from fastapi.testclient import TestClient

            from app.main import app

            _LazyTestClient._real = TestClient(app)
        return getattr(_LazyTestClient._real, attr)


client = _LazyTestClient()


def _batch(db_session, **overrides) -> IngestBatch:
    defaults = {
        "source_type": IngestBatchSourceType.EMAIL,
        "status": IngestBatchStatus.PENDING,
        "subject": "Redesign test batch",
    }
    defaults.update(overrides)
    batch = IngestBatch(**defaults)
    db_session.add(batch)
    db_session.flush()
    return batch


@pytest.mark.integration
def test_page_renders_redesigned_header(db_session):
    """Header chrome shows the new title + chips, not the legacy filter dropdown."""
    response = client.get("/triage")
    assert response.status_code == 200
    text = response.text
    assert "Triage" in text
    assert "Needs classification" in text
    assert "Add document" in text
    # The legacy originator-filter dropdown is gone.
    assert "Originator" not in text or "filter_list" not in text


@pytest.mark.integration
def test_row_renders_for_each_bundle(db_session):
    """Every bundle is rendered as a triage-row-{key} element."""
    batch = _batch(db_session)
    doc = Document(
        title="Row test doc",
        ingest_batch_id=batch.id,
        case_id="_TRIAGE",
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/triage")
    assert response.status_code == 200
    assert f'id="triage-row-batch-{batch.id}"' in response.text
    assert f'data-triage-row-id="batch-{batch.id}"' in response.text


@pytest.mark.integration
def test_row_carries_mock_status_attribute(db_session):
    """Row exposes its mock_status so the chip filter can `x-show` against it."""
    batch = _batch(db_session)
    doc = Document(
        title="Stuck doc",
        ingest_batch_id=batch.id,
        case_id="_TRIAGE",
        needs_review=True,
        pipeline_state=PipelineState.FAILED,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/triage").text
    assert 'data-mock-status="stuck"' in response


@pytest.mark.integration
def test_inline_expand_body_present_for_each_row(db_session):
    """Each row carries an inline expand body keyed by bundle.key."""
    batch = _batch(db_session)
    doc = Document(
        title="Expand test doc",
        ingest_batch_id=batch.id,
        case_id="_TRIAGE",
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/triage")
    assert f'id="triage-row-expanded-batch-{batch.id}"' in response.text
    assert f'id="triage-row-expanded-body-batch-{batch.id}"' in response.text
    assert "Bundle contents" in response.text


@pytest.mark.integration
def test_drawer_mount_present_for_each_row(db_session):
    """Each row carries a drawer chrome behind a `<template x-if>` guard."""
    batch = _batch(db_session)
    doc = Document(
        title="Drawer test doc",
        ingest_batch_id=batch.id,
        case_id="_TRIAGE",
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/triage").text
    assert f'id="triage-drawer-body-batch-{batch.id}"' in response
    assert "triageDrawer(" in response


@pytest.mark.integration
def test_filter_chips_taxonomy(db_session):
    """The filter chips render All / Needs classification / Needs review / Stuck / Processing."""
    response = client.get("/triage").text
    for label in (
        "All",
        "Needs classification",
        "Needs review",
        "Stuck",
        "Processing",
    ):
        assert label in response


@pytest.mark.integration
def test_legacy_card_ids_are_gone(db_session):
    """No `triage-card-`, `triage-bundle-group-`, or `triage-bundle-badge-` IDs remain."""
    batch = _batch(db_session)
    doc = Document(
        title="Legacy id test",
        ingest_batch_id=batch.id,
        case_id="_TRIAGE",
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get("/triage").text
    assert "triage-card-" not in response
    assert "triage-bundle-group-" not in response
    assert "triage-bundle-badge-" not in response


@pytest.mark.integration
def test_polling_endpoint_returns_row_oob(db_session):
    """`/triage/card/{doc_id}/live` returns the new row-targeted OOB markup."""
    batch = _batch(db_session)
    doc = Document(
        title="Polling test doc",
        ingest_batch_id=batch.id,
        case_id="_TRIAGE",
        needs_review=True,
        pipeline_state=PipelineState.RUNNING,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get(f"/triage/card/{doc.id}/live")
    assert response.status_code == 200
    assert f'id="triage-row-batch-{batch.id}"' in response.text


@pytest.mark.integration
def test_bundle_endpoint_returns_row_html(db_session):
    """`/triage/bundle/{batch_id}` now returns triage_row.html (not the deleted bundle template)."""
    batch = _batch(db_session)
    doc = Document(
        title="Bundle endpoint doc",
        ingest_batch_id=batch.id,
        case_id="_TRIAGE",
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get(f"/triage/bundle/{batch.id}")
    assert response.status_code == 200
    assert f'id="triage-row-batch-{batch.id}"' in response.text


# ---------------------------------------------------------------------------
# Iteration 2 — triage doc HUD partial
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_triage_doc_hud_route_returns_partial(db_session):
    """`GET /triage/doc/{id}/hud` returns the new triage doc HUD partial."""
    doc = Document(
        title="Doc HUD route test",
        case_id="_TRIAGE",
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    response = client.get(f"/triage/doc/{doc.id}/hud")
    assert response.status_code == 200
    assert 'data-hud-context="embedded"' in response.text
    assert 'data-hud-mode="review"' in response.text


@pytest.mark.integration
def test_triage_doc_hud_two_column_metadata(db_session):
    """The metadata review block is the new 2-column read-only grid."""
    doc = Document(
        title="Two-col metadata doc",
        case_id="_TRIAGE",
        needs_review=False,  # exercise the confirmed read-only path
    )
    db_session.add(doc)
    db_session.commit()

    text = client.get(f"/triage/doc/{doc.id}/hud").text
    assert "Metadata review" in text
    # 2-col grid wrapper
    assert "grid-cols-2" in text
    # The 7 fields kept in the read-only metadata grid (Title moved to the
    # active doc header; Case + Case ID moved to the case selector above).
    for label in ("Originator", "Sender", "Issued", "Received", "Tier", "Type", "AZ"):
        assert label in text, f"metadata review missing {label}"


@pytest.mark.integration
def test_triage_doc_hud_includes_pipeline_and_case(db_session):
    """Pipeline + Case section heads are present per scope decision."""
    doc = Document(
        title="Pipeline + case sections",
        case_id="_TRIAGE",
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    text = client.get(f"/triage/doc/{doc.id}/hud").text
    assert "Pipeline" in text
    assert "Case" in text


@pytest.mark.integration
def test_triage_doc_hud_grid_wraps_data_sections(db_session):
    """Relationships / Grounds / Actions / Cost Delta render in a single-column grid."""
    doc = Document(
        title="Grid wrap test",
        case_id="_TRIAGE",
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    text = client.get(f"/triage/doc/{doc.id}/hud").text
    # Grid wrapper is now single-column (no responsive 2-col breakpoint).
    assert "grid-cols-1" in text
    assert "md:grid-cols-2" not in text


@pytest.mark.integration
def test_triage_row_expanded_fetches_new_hud_endpoint(db_session):
    """The inline expand body's hx-get points at the new /triage/doc/{id}/hud route."""
    batch = _batch(db_session)
    doc = Document(
        title="Expand wires new endpoint",
        ingest_batch_id=batch.id,
        case_id="_TRIAGE",
        needs_review=True,
    )
    db_session.add(doc)
    db_session.commit()

    text = client.get("/triage").text
    assert f'hx-get="/triage/doc/{doc.id}/hud"' in text
    # Old context=triage path should no longer appear in the page markup.
    assert "?context=triage" not in text

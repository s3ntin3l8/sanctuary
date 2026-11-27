"""Pipeline stages and the case brief record the model and prompt version behind them."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import text as _sa_text

from app.models.database import Case, Claim, ClaimEvidence
from app.models.enums import (
    ClaimEvidenceRole,
    ClaimStatus,
    PipelineStage,
    SignificanceTier,
)
from app.services.case_overview_service import serialize_brief
from app.services.intelligence import _ai_call, ai_provenance
from app.services.intelligence.prompts import PROMPT_VERSION
from app.services.pipeline_status import mark_completed, mark_started
from app.tasks.document_processing import process_document_task
from app.tasks.enrich_document import enrich_document_task
from app.tasks.extract_entities import extract_entities_task


def _stage_row(db, doc_id: int, stage: PipelineStage):
    return db.execute(
        _sa_text(
            "SELECT status, model, provider, prompt_version "
            "FROM document_pipeline_stages WHERE document_id = :d AND stage = :s"
        ),
        {"d": doc_id, "s": stage.value},
    ).one()


@pytest.fixture
def fake_ai():
    """call_json_ai runs for real; only the provider and the stream are stubbed."""

    class FakeCfg:
        summary_model = "qwen3.5-9b"

    async def fake_get_type():
        return "lmstudio"

    async def fake_params(*, model, prompt, system_prompt, stream, options):
        return {"url": "http://stub", "json": {"model": model}, "headers": {}}

    def fake_stream(**_kwargs):
        return '{"ok": true}', ""

    with (
        patch.object(_ai_call, "get_chat_config", lambda _db: FakeCfg()),
        patch.object(_ai_call.chat_provider, "get_type", fake_get_type, create=True),
        patch.object(
            _ai_call.chat_provider, "get_generate_params", fake_params, create=True
        ),
        patch.object(
            _ai_call.chat_provider,
            "reload_from_db",
            lambda *_a, **_k: None,
            create=True,
        ),
        patch(
            "app.services.user_settings_service.get_ai_debug_capture",
            return_value=False,
        ),
        patch.object(_ai_call, "_stream_response", side_effect=fake_stream),
    ):
        yield


@pytest.mark.unit
def test_entities_stage_records_model_and_prompt_version(
    db_session, sample_document, fake_ai
):
    """The ENTITIES task stamps its stage row with the model of the AI call it made."""
    db_session.execute(
        _sa_text(
            "INSERT INTO document_pipeline_stages (document_id, stage, status) "
            "VALUES (:d, 'enrich', 'completed'), (:d, 'entities', 'pending')"
        ),
        {"d": sample_document.id},
    )
    sample_document.ai_summary_created_at = datetime.now(UTC)
    db_session.commit()

    def fake_extract(doc_id):
        _ai_call.call_json_ai(
            system_prompt="s",
            user_prompt="u",
            options={},
            debug_label=f"doc_{doc_id}_entities",
        )
        return None

    with (
        patch("app.dependencies.get_db_session", return_value=db_session),
        patch(
            "app.services.intelligence.entity_extractor.extract",
            side_effect=fake_extract,
        ),
        patch.object(db_session, "close", return_value=None),
    ):
        result = extract_entities_task.run(sample_document.id)

    assert result["status"] == "success"
    row = _stage_row(db_session, sample_document.id, PipelineStage.ENTITIES)
    assert row.status == "completed"
    assert row.model == "qwen3.5-9b"
    assert row.provider == "lmstudio"
    assert row.prompt_version == PROMPT_VERSION


@pytest.mark.unit
def test_enrich_stage_records_model_and_prompt_version(
    db_session, sample_document, fake_ai
):
    """The ENRICH task stamps its stage row with the model of the AI call it made."""
    db_session.execute(
        _sa_text(
            "INSERT INTO document_pipeline_stages (document_id, stage, status) "
            "VALUES (:d, 'batch_analysis', 'completed'), (:d, 'enrich', 'pending')"
        ),
        {"d": sample_document.id},
    )
    db_session.commit()

    def fake_enrich(doc_id):
        _ai_call.call_json_ai(
            system_prompt="s",
            user_prompt="u",
            options={},
            debug_label=f"doc_{doc_id}_enrich",
        )

    with (
        patch("app.dependencies.get_db_session", return_value=db_session),
        patch(
            "app.services.intelligence.document_enricher.enrich",
            side_effect=fake_enrich,
        ),
        patch("app.tasks.enrich_document._link_copies", return_value=None),
        patch("app.tasks.enrich_document._dispatch_relationships"),
        patch("app.tasks.enrich_document._dispatch_if_pending"),
        patch("app.tasks.enrich_document.release_relationship_gate"),
        patch("app.tasks.enrich_document._trigger_cost_rollup"),
        patch.object(db_session, "close", return_value=None),
    ):
        result = enrich_document_task.run(sample_document.id)

    assert result["status"] == "success"
    row = _stage_row(db_session, sample_document.id, PipelineStage.ENRICH)
    assert row.status == "completed"
    assert (row.model, row.provider, row.prompt_version) == (
        "qwen3.5-9b",
        "lmstudio",
        PROMPT_VERSION,
    )


@pytest.mark.unit
def test_extract_records_the_ocr_engine_without_a_prompt_version(
    db_session, sample_document
):
    """EXTRACT stores the OCR model and engine; a text read stores nothing."""

    def fake_process(doc, _db):
        doc.meta = {
            "extractor": "chandra-ocr-2",
            "ocr_model": "chandra-ocr-2-8b",
            "chunks": [],
        }

    with (
        patch("app.tasks.document_processing.get_db_session", return_value=db_session),
        patch(
            "app.services.ingestion.service.process_uploaded_document",
            side_effect=fake_process,
        ),
        patch("app.tasks.document_processing._trigger_metadata_phase_barrier"),
        patch.object(db_session, "close", return_value=None),
    ):
        process_document_task.run(sample_document.id)

    row = _stage_row(db_session, sample_document.id, PipelineStage.EXTRACT)
    assert row.status == "completed"
    assert (row.model, row.provider, row.prompt_version) == (
        "chandra-ocr-2-8b",
        "chandra",
        None,
    )


@pytest.mark.unit
def test_completion_outside_a_started_scope_keeps_stored_provenance(
    db_session, sample_document
):
    """A re-dispatch that completes a stage it never started (no AI call of its
    own) must not wipe the model an earlier run recorded."""
    mark_started(sample_document.id, PipelineStage.ENRICH, db_session)
    ai_provenance.note("qwen3.5-9b", "lmstudio")
    mark_completed(sample_document.id, PipelineStage.ENRICH, db_session)

    ai_provenance.begin(-1)  # another document's stage opened a fresh scope
    mark_completed(sample_document.id, PipelineStage.ENRICH, db_session)

    row = _stage_row(db_session, sample_document.id, PipelineStage.ENRICH)
    assert row.model == "qwen3.5-9b"


@pytest.mark.unit
def test_restarted_stage_without_ai_call_clears_old_model(db_session, sample_document):
    mark_started(sample_document.id, PipelineStage.ENRICH, db_session)
    ai_provenance.note("qwen3.5-9b", "lmstudio")
    mark_completed(sample_document.id, PipelineStage.ENRICH, db_session)

    mark_started(sample_document.id, PipelineStage.ENRICH, db_session)
    mark_completed(sample_document.id, PipelineStage.ENRICH, db_session)

    row = _stage_row(db_session, sample_document.id, PipelineStage.ENRICH)
    assert (row.model, row.provider, row.prompt_version) == (None, None, None)


@pytest.mark.unit
def test_starts_before_one_call_share_its_provenance(db_session, sample_case):
    """BATCH_ANALYSIS starts every document of the batch, then makes one call."""
    from app.models.database import Document

    docs = [Document(title=f"d{i}", case_id=sample_case.id) for i in range(2)]
    db_session.add_all(docs)
    db_session.commit()
    for d in docs:
        mark_started(d.id, PipelineStage.BATCH_ANALYSIS, db_session)
    ai_provenance.note("qwen3.5-9b", "ollama")
    for d in docs:
        mark_completed(d.id, PipelineStage.BATCH_ANALYSIS, db_session)

    for d in docs:
        row = _stage_row(db_session, d.id, PipelineStage.BATCH_ANALYSIS)
        assert (row.model, row.provider) == ("qwen3.5-9b", "ollama")


def _run_brief(case_id: str):
    from app.services.intelligence import case_brief_generator

    class FakeCfg:
        summary_model = "qwen3.5-9b"

    with (
        patch.object(case_brief_generator, "get_chat_config", lambda _db: FakeCfg()),
        patch(
            "app.services.ai_provider.chat_provider.reload_from_db",
            lambda *_a, **_k: None,
        ),
        patch.object(
            case_brief_generator,
            "_call_brief_sync",
            return_value={
                "posture": "p",
                "pressure_points": [],
                "next_move": "n",
                "detected_status": None,
                "status_rationale": "",
            },
        ),
    ):
        case_brief_generator.generate(case_id)


@pytest.mark.unit
def test_brief_records_provenance_and_goes_stale_on_claim_status_change(
    db_session, sample_case, sample_document
):
    claim = Claim(claim_text="Die Miete wurde gezahlt.", status=ClaimStatus.ASSERTED)
    db_session.add(claim)
    db_session.flush()
    db_session.add(
        ClaimEvidence(
            claim_id=claim.id,
            document_id=sample_document.id,
            role=ClaimEvidenceRole.ASSERTS,
        )
    )
    db_session.commit()

    _run_brief(sample_case.id)

    db_session.expire_all()
    case = db_session.get(Case, sample_case.id)
    assert case.ai_brief_model == "qwen3.5-9b"
    assert case.ai_brief_prompt_version == PROMPT_VERSION
    assert case.ai_brief_inputs_hash
    view = serialize_brief(db_session, case)
    assert view.stale is False
    assert view.model == "qwen3.5-9b"

    claim = db_session.get(Claim, claim.id)
    claim.status = ClaimStatus.CONTESTED
    db_session.commit()
    assert serialize_brief(db_session, case).stale is True

    _run_brief(sample_case.id)
    db_session.expire_all()
    case = db_session.get(Case, sample_case.id)
    assert serialize_brief(db_session, case).stale is False


@pytest.mark.unit
def test_brief_goes_stale_when_a_document_tier_changes(
    db_session, sample_case, sample_document
):
    _run_brief(sample_case.id)
    db_session.expire_all()
    case = db_session.get(Case, sample_case.id)
    assert serialize_brief(db_session, case).stale is False

    sample_document.significance_tier = SignificanceTier.CRITICAL
    db_session.commit()
    assert serialize_brief(db_session, case).stale is True


@pytest.mark.unit
def test_brief_without_recorded_hash_is_not_stale(db_session, sample_case):
    sample_case.ai_brief = {"posture": "old"}
    db_session.commit()
    assert serialize_brief(db_session, sample_case).stale is False

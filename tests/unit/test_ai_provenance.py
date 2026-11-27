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
from app.services.pipeline_status import (
    mark_completed,
    mark_failed,
    mark_started,
    reset_stage,
)
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
    own) must not wipe the model an earlier run recorded, even in the same
    task: completing a document takes it out of the scope."""
    mark_started(sample_document.id, PipelineStage.ENRICH, db_session)
    ai_provenance.note("qwen3.5-9b", "lmstudio")
    mark_completed(sample_document.id, PipelineStage.ENRICH, db_session)

    mark_completed(sample_document.id, PipelineStage.ENRICH, db_session)

    row = _stage_row(db_session, sample_document.id, PipelineStage.ENRICH)
    assert row.model == "qwen3.5-9b"


@pytest.mark.unit
def test_a_document_started_without_a_call_never_adopts_a_later_tasks_model(
    db_session, sample_case
):
    """The reviewer's case: a stage starts doc A and makes no AI call (text-only
    EXTRACT, EMBEDDINGS); the next task starts doc B and calls a model. A
    later completion of A outside a started scope must keep A's stored values."""
    from app.models.database import Document
    from app.tasks.celery_app import _pop_ai_provenance, _push_ai_provenance

    a, b = (
        Document(title="a", case_id=sample_case.id),
        Document(title="b", case_id=sample_case.id),
    )
    db_session.add_all([a, b])
    db_session.commit()

    _push_ai_provenance(task_id="t1")  # task 1 starts A, never calls a model
    mark_started(a.id, PipelineStage.EMBEDDINGS, db_session)
    _pop_ai_provenance(task_id="t1")

    _push_ai_provenance(task_id="t2")  # task 2 reads B with Chandra
    mark_started(b.id, PipelineStage.EXTRACT, db_session)
    ai_provenance.note("chandra-ocr-2-8b", "chandra", prompt_version=None)
    mark_completed(b.id, PipelineStage.EXTRACT, db_session)

    mark_completed(a.id, PipelineStage.EMBEDDINGS, db_session)  # recovery sweep
    row = _stage_row(db_session, a.id, PipelineStage.EMBEDDINGS)
    assert row.model is None

    # Within one task too: a failed or skipped start leaves the scope.
    mark_started(a.id, PipelineStage.ENTITIES, db_session)
    mark_failed(a.id, PipelineStage.ENTITIES, db_session, error="boom")
    mark_started(b.id, PipelineStage.ENTITIES, db_session)
    ai_provenance.note("qwen3.5-9b", "lmstudio")
    mark_completed(a.id, PipelineStage.ENTITIES, db_session)
    assert _stage_row(db_session, a.id, PipelineStage.ENTITIES).model is None
    _pop_ai_provenance(task_id="t2")


@pytest.mark.unit
def test_an_eager_task_inside_a_stage_leaves_the_stage_scope_intact(
    db_session, sample_document
):
    """With CELERY_TASK_ALWAYS_EAGER a task dispatched mid-stage runs inside the
    enclosing task; its own scope must not replace the enclosing one."""
    from app.tasks.celery_app import _pop_ai_provenance, _push_ai_provenance

    _push_ai_provenance(task_id="outer")
    mark_started(sample_document.id, PipelineStage.ENRICH, db_session)
    ai_provenance.note("qwen3.5-9b", "lmstudio")

    _push_ai_provenance(task_id="inner")  # eager child task
    ai_provenance.note("other-model", "ollama")
    _pop_ai_provenance(task_id="inner")

    mark_completed(sample_document.id, PipelineStage.ENRICH, db_session)
    _pop_ai_provenance(task_id="outer")
    row = _stage_row(db_session, sample_document.id, PipelineStage.ENRICH)
    assert (row.model, row.provider) == ("qwen3.5-9b", "lmstudio")


@pytest.mark.unit
def test_failure_and_reset_clear_the_previous_runs_model(db_session, sample_document):
    mark_started(sample_document.id, PipelineStage.ENRICH, db_session)
    ai_provenance.note("qwen3.5-9b", "lmstudio")
    mark_completed(sample_document.id, PipelineStage.ENRICH, db_session)
    mark_failed(sample_document.id, PipelineStage.ENRICH, db_session, error="boom")
    assert (
        _stage_row(db_session, sample_document.id, PipelineStage.ENRICH).model is None
    )

    mark_started(sample_document.id, PipelineStage.ENRICH, db_session)
    ai_provenance.note("qwen3.5-9b", "lmstudio")
    mark_completed(sample_document.id, PipelineStage.ENRICH, db_session)
    reset_stage(sample_document.id, PipelineStage.ENRICH, db_session)
    db_session.commit()
    row = _stage_row(db_session, sample_document.id, PipelineStage.ENRICH)
    assert (row.status, row.model) == ("pending", None)


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


@pytest.mark.unit
def test_brief_goes_stale_when_a_deadline_changes(
    db_session, sample_case, sample_deadline
):
    """The chronology the brief reads carries action items: moving or ticking
    off a deadline is an input change."""
    _run_brief(sample_case.id)
    db_session.expire_all()
    case = db_session.get(Case, sample_case.id)
    assert serialize_brief(db_session, case).stale is False

    from app.models.database import ActionItem
    from app.models.enums import ActionItemStatus

    item = db_session.get(ActionItem, sample_deadline.id)
    item.status = ActionItemStatus.COMPLETED
    db_session.commit()
    assert serialize_brief(db_session, case).stale is True


@pytest.mark.unit
def test_brief_hash_reads_every_summary_shape(db_session, sample_case, sample_document):
    """A ``[{kind, text}]`` summary (and a plain string) is hashed by its legal
    bullet, not refused."""
    from app.services.intelligence.brief_inputs import brief_inputs_hash

    sample_document.ai_summary = [{"kind": "legal", "text": "Ruling for the client."}]
    db_session.commit()
    first = brief_inputs_hash(db_session, sample_case)
    sample_document.ai_summary = [{"kind": "legal", "text": "Ruling against."}]
    db_session.commit()
    assert brief_inputs_hash(db_session, sample_case) != first
    sample_document.ai_summary = "Plain text summary."
    db_session.commit()
    assert brief_inputs_hash(db_session, sample_case)

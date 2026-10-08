import logging
import time

import httpx
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import OperationalError as SA_OperationalError

from app.config import EXTRACT_TASK_SOFT_TIME_LIMIT, EXTRACT_TASK_TIME_LIMIT
from app.dependencies import get_db_session
from app.models.database import Document
from app.models.enums import PipelineStage
from app.services.intelligence._ai_call import is_transient_ai_http_error
from app.services.model_gate import ModelGateTimeout
from app.services.pipeline_status import (
    METADATA_FAILURE_CASCADE,
    is_db_locked,
    stages_dict,
)
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# Celery-level retries when metadata_task cannot get the model slot within the
# gate's own 30-minute acquire timeout (#143). A scan backlog keeps the chandra
# (OCR) family busy for a long time, and the gate's drain-first bias makes qwen
# wait behind it, so one 30-minute wait is not proof the doc is unprocessable.
# Failing the doc outright (the old behaviour) cascaded ENRICH/RELATIONSHIPS/
# CLAIMS/ENTITIES to FAILED over what is just queueing. 4 retries × (up to
# 30 min gate wait + 5 min pause) bound the total wait at ~2.5-3 h before the
# doc is finally failed. This is deliberately NOT part of the in-process retry
# loop: three more 30-minute waits would blow the task's soft time limit.
_GATE_RETRY_MAX = 4
_GATE_RETRY_COUNTDOWN = 5 * 60

_TRANSIENT_AI_ERRORS = (
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ReadError,
)


def dispatch_metadata_phase(batch_id: int, db) -> None:
    """Dispatch metadata_task for every doc in a batch at once.

    The winning side of the OCR->chat barrier (see
    claim_batch_for_metadata_phase). METADATA is self_claims=True (see
    STAGE_REGISTRY) — its own dispatcher hands it off unclaimed and
    metadata_task CAS-claims its own doc's METADATA stage on entry, no-oping
    for docs whose EXTRACT failed/skipped or whose METADATA already ran. So
    this just fans out .delay() to every doc in the batch; no pre-claim.
    """
    doc_ids = [
        row[0]
        for row in db.query(Document.id)
        .filter(Document.ingest_batch_id == batch_id)
        .all()
    ]
    for sibling_id in doc_ids:
        metadata_task.delay(sibling_id)


def _trigger_metadata_phase_barrier(doc_id: int, batch_id: int | None, db) -> None:
    """Check/dispatch the OCR->chat barrier after an EXTRACT terminal exit.

    Call this on every terminal exit of process_document_task — success AND
    terminal failure — since claim_batch_for_metadata_phase's readiness
    predicate treats a failed/skipped EXTRACT as terminal too; only a
    doc still pending/retrying blocks the claim. Docs with no batch (rare —
    only when ingest_batch_id was never set) skip the barrier and dispatch
    directly, since there's no sibling set to wait for.

    Also opportunistically flips the batch to FAILED once every document's
    EXTRACT has terminally failed (mark_batch_failed_if_all_extracts_failed
    is a no-op otherwise) — this is the natural "an EXTRACT just went
    terminal for this batch" hook, so it's reused for that check too.

    Never raises: a crash right here (before the claim/dispatch completes)
    leaves the batch's metadata_phase_queued_at NULL, which
    recover_unclaimed_ready_metadata_phases picks up on the next maintenance
    sweep — so letting an exception here mask the caller's own terminal-state
    transition (return/raise) would trade a self-healing gap for a bigger one.
    """
    try:
        if not batch_id:
            metadata_task.delay(doc_id)
            return
        from app.services.intelligence.orchestrator import (
            claim_batch_for_metadata_phase,
            mark_batch_failed_if_all_extracts_failed,
        )

        mark_batch_failed_if_all_extracts_failed(batch_id, db)

        if claim_batch_for_metadata_phase(batch_id, db):
            dispatch_metadata_phase(batch_id, db)
    except Exception:
        logger.error(
            "Doc #%d: metadata-phase barrier check failed for batch %s — "
            "will be picked up by recover_unclaimed_ready_metadata_phases",
            doc_id,
            batch_id,
            exc_info=True,
        )


@celery_app.task(
    bind=True,
    max_retries=3,
    time_limit=EXTRACT_TASK_TIME_LIMIT,
    soft_time_limit=EXTRACT_TASK_SOFT_TIME_LIMIT,
)
def process_document_task(self, doc_id: int):
    """EXTRACT-only: run Docling conversion, then hand off METADATA to the ai queue.

    This task is pinned to the `ingest` queue (concurrency configurable via
    Settings, default 4 — see get_ocr_concurrency) so heavy Docling/OCR work
    doesn't share a slot with LLM calls. On every terminal
    exit (success or failure) it checks the OCR->chat barrier
    (claim_batch_for_metadata_phase): once every doc in the batch has a
    terminal EXTRACT, metadata_task is dispatched for the whole batch at
    once, rather than each doc triggering its own chat call the instant its
    own EXTRACT finishes — this is what keeps the shared inference host from
    swapping between the OCR and chat models mid-batch.
    """
    from app.services.pipeline_status import (
        mark_completed,
        mark_failed_with_cascade,
        mark_started,
        schedule_retry,
    )

    logger.info("Doc #%d: processing task started", doc_id)
    db = get_db_session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"Document {doc_id} not found")
            return {"status": "not_found", "doc_id": doc_id}

        from app.services.ingestion.service import (
            IngestionError,
            process_uploaded_document,
        )

        # Idempotency guard: if a prior run already extracted content (meta has
        # the extractor stamp + chunks), don't re-call Chandra. Reconcile the
        # stage row to COMPLETED. Only re-check the barrier if the cascade
        # is incomplete — for already-completed docs, metadata_task would be a
        # no-op chain through every stage's claim CAS, just adding log noise.
        # Defends against duplicate dispatches (e.g. recover_stuck_pending_-
        # dispatches racing the FIFO ingest queue).
        meta = doc.meta or {}
        if meta.get("extractor") and meta.get("chunks"):
            mark_completed(doc_id, PipelineStage.EXTRACT, db)
            if doc.pipeline_state != "completed":
                _trigger_metadata_phase_barrier(doc_id, doc.ingest_batch_id, db)
            logger.info(
                "Doc #%d: already extracted (extractor=%s, %d chunks) — skipping Chandra",
                doc_id,
                meta.get("extractor"),
                len(meta.get("chunks") or []),
            )
            return {"status": "skipped_already_extracted", "doc_id": doc_id}

        mark_started(doc_id, PipelineStage.EXTRACT, db)
        try:
            process_uploaded_document(doc, db)
            mark_completed(doc_id, PipelineStage.EXTRACT, db)
            logger.info(f"Document {doc_id} extracted successfully")
        except IngestionError as e:
            db.rollback()
            error_msg = f"Ingestion error: {e.message}"
            if e.detail:
                error_msg += f" ({e.detail})"
            mark_failed_with_cascade(doc_id, PipelineStage.EXTRACT, db, error=error_msg)
            logger.warning(f"Document {doc_id} ingestion failed: {e}")
            _trigger_metadata_phase_barrier(doc_id, doc.ingest_batch_id, db)
            return {"status": "failed", "doc_id": doc_id, "error": str(e)}
        except SoftTimeLimitExceeded as e:
            # celery.exceptions.SoftTimeLimitExceeded is an Exception
            # subclass — without this explicit branch ahead of the generic
            # one below, it would be treated as a retryable system error,
            # racing self.retry()'s countdown against the imminent hard
            # task_time_limit kill. No retry: the soft limit means "wrap up
            # now," so cascade the failure and return cleanly instead.
            db.rollback()
            logger.error(f"Document {doc_id} soft time limit exceeded: {e}")
            mark_failed_with_cascade(
                doc_id,
                PipelineStage.EXTRACT,
                db,
                error=f"soft time limit exceeded: {e}",
            )
            _trigger_metadata_phase_barrier(doc_id, doc.ingest_batch_id, db)
            return {"status": "failed", "doc_id": doc_id, "error": str(e)}
        except Exception as e:
            db.rollback()
            logger.error(f"Document {doc_id} processing failed: {e}", exc_info=True)

            if self.request.retries < self.max_retries:
                countdown = 60 * (self.request.retries + 1)
                # Mark RETRYING (not FAILED) so the polling templates keep
                # refreshing through the countdown — the next attempt runs
                # invisibly otherwise. EXTRACT isn't terminal yet, so the
                # barrier predicate can't be satisfied for this doc — skip it.
                schedule_retry(
                    doc_id,
                    PipelineStage.EXTRACT,
                    db,
                    error=f"System error: {e}",
                    attempt=self.request.retries + 1,
                    max_attempts=self.max_retries,
                    countdown=countdown,
                )
                raise self.retry(exc=e, countdown=countdown) from e

            # All retries exhausted — terminal failure cascades downstream.
            mark_failed_with_cascade(
                doc_id, PipelineStage.EXTRACT, db, error=f"System error: {e}"
            )
            _trigger_metadata_phase_barrier(doc_id, doc.ingest_batch_id, db)
            from celery.exceptions import MaxRetriesExceededError

            raise MaxRetriesExceededError(
                f"Document {doc_id} failed after {self.max_retries} retries",
                exc=e,
            ) from e
        except BaseException as e:
            db.rollback()
            mark_failed_with_cascade(
                doc_id,
                PipelineStage.EXTRACT,
                db,
                error=f"task aborted: {type(e).__name__}",
            )
            _trigger_metadata_phase_barrier(doc_id, doc.ingest_batch_id, db)
            raise

        # EXTRACT done — check the barrier and return so the ingest worker
        # slot is free for the next doc's EXTRACT.
        _trigger_metadata_phase_barrier(doc_id, doc.ingest_batch_id, db)
        return {"status": "success", "doc_id": doc_id}
    finally:
        db.close()


@celery_app.task(
    bind=True,
    name="app.tasks.document_processing.metadata_task",
    queue="ai",
    max_retries=_GATE_RETRY_MAX,
)
def metadata_task(self, doc_id: int):
    """Phase 1 metadata (LLM) + downstream fan-out (batch analysis + embeddings).

    Runs on the `ai` queue (concurrency=3) so sibling docs can have their
    LLM calls in flight at the same time without blocking the ingest slot
    that does Docling/OCR.
    """
    from app.services.pipeline_status import (
        claim_stage_for_dispatch,
        mark_completed,
        mark_failed_with_cascade,
    )

    db = get_db_session()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            logger.warning(f"metadata_task: document {doc_id} not found")
            return {"status": "not_found", "doc_id": doc_id}

        stages = stages_dict(doc)
        metadata_done = (
            stages.get(PipelineStage.METADATA.value, {}).get("status") == "completed"
        )
        if not metadata_done:
            # Atomic CAS: only the worker that flips METADATA from PENDING→RUNNING
            # proceeds. Defends against the race between process_document_task's
            # post-EXTRACT dispatch and recover_stuck_pending_dispatches firing
            # the same metadata_task seconds later — without this both runs would
            # call _run_phase1_summary and burn two LLM round-trips for the same
            # doc. If the claim fails (concurrent runner won, or stage already
            # running/retrying/failed) we return early; the winning runner owns
            # the downstream fan-out so we must not double-dispatch it either.
            # A Celery retry (retries > 0) is a continuation of our own claim:
            # the stage is RETRYING then, which the pending→running CAS rejects.
            if self.request.retries == 0 and not claim_stage_for_dispatch(
                doc_id, PipelineStage.METADATA, db
            ):
                logger.info(
                    "Doc #%d: METADATA already claimed by another worker — skipping",
                    doc_id,
                )
                return {"status": "already_claimed", "doc_id": doc_id}
            try:
                _run_phase1_summary(doc_id)
            except ModelGateTimeout as exc:
                _retry_or_fail_after_gate_timeout(self, doc_id, exc)

        # Re-read after _run_phase1_summary, which manages its own DB sessions.
        db.refresh(doc)
        metadata_status = (
            stages_dict(doc)
            .get(PipelineStage.METADATA.value, {})
            .get("status", "pending")
        )
        metadata_failed = metadata_status == "failed"
        if metadata_failed:
            # Don't return early: a terminal (completed-or-failed) METADATA
            # still counts toward claim_batch_for_analysis's readiness check,
            # and this doc may be the last sibling the batch is waiting on.
            # Returning here used to leave the batch permanently unclaimed
            # until the 5-minute recovery sweep picked it up. This doc's own
            # downstream (ENRICH/RELATIONSHIPS/CLAIMS/ENTITIES) is already
            # terminal via mark_failed_with_cascade in _run_phase1_summary,
            # so the embeddings dispatch below is still skipped for it.
            logger.warning(
                f"Doc {doc_id}: METADATA failed after retries — "
                "attempting batch claim, then stopping"
            )

        # Batch analysis gate: when every doc in this batch has completed METADATA,
        # claim and dispatch the batch analyzer. Uses an atomic CAS so only one
        # worker fires the task even when multiple docs finish near-simultaneously.
        if doc.ingest_batch_id:
            from app.services.intelligence.orchestrator import claim_batch_for_analysis

            db_batch = get_db_session()
            try:
                if claim_batch_for_analysis(doc.ingest_batch_id, db_batch):
                    from app.tasks.analyze_batch import analyze_batch_task

                    analyze_batch_task.delay(doc.ingest_batch_id)
                    logger.info(
                        "Doc #%d: batch #%d ready — dispatched analyze_batch_task",
                        doc_id,
                        doc.ingest_batch_id,
                    )
                else:
                    # Claim failed — either someone else won the race, or the
                    # batch is already analyzed (idempotency guard fired because
                    # a doc in the batch has batch_analysis terminal). That doc
                    # may be a sibling (this doc's batch_analysis was
                    # cascade-reset to pending by a metadata retry but will
                    # never be claimed) or this doc itself (single-doc batch:
                    # batch_analysis was SKIPPED on the first run). Detect it
                    # and promote + dispatch enrich directly so the doc isn't
                    # permanently stranded.
                    from sqlalchemy import text

                    batch_already_done = db_batch.execute(
                        text(
                            """
                            SELECT 1 FROM documents d2
                            JOIN document_pipeline_stages dps2
                              ON dps2.document_id = d2.id
                            WHERE d2.ingest_batch_id = :bid
                              AND dps2.stage = 'batch_analysis'
                              AND dps2.status IN ('completed', 'failed', 'skipped')
                            LIMIT 1
                            """
                        ),
                        {"bid": doc.ingest_batch_id},
                    ).scalar()
                    if batch_already_done:
                        # Don't rewrite a stage that is already terminal (a
                        # SKIPPED single-doc analysis must stay SKIPPED).
                        own_batch_stage = (
                            stages_dict(doc)
                            .get(PipelineStage.BATCH_ANALYSIS.value, {})
                            .get("status")
                        )
                        if own_batch_stage not in ("completed", "failed", "skipped"):
                            mark_completed(
                                doc_id, PipelineStage.BATCH_ANALYSIS, db_batch
                            )
                        logger.info(
                            "Doc #%d: batch #%d already analyzed — promoted "
                            "batch_analysis to completed, dispatching enrich directly",
                            doc_id,
                            doc.ingest_batch_id,
                        )
                        if claim_stage_for_dispatch(
                            doc_id, PipelineStage.ENRICH, db_batch
                        ):
                            from app.tasks.enrich_document import enrich_document_task

                            enrich_document_task.delay(doc_id)
            finally:
                db_batch.close()

        if metadata_failed:
            # This doc's own downstream is already terminal (cascaded in
            # _run_phase1_summary) — make sure the case brief CAS gets
            # poked for it, since nothing else on this path will. Without
            # this, a doc whose METADATA fails and who happens to be the
            # last sibling to reach a CLAIMS-terminal state would never
            # trigger the brief.
            from app.services.intelligence.orchestrator import (
                trigger_case_brief_if_ready,
            )

            trigger_case_brief_if_ready(doc_id)
            return {"status": "metadata_failed", "doc_id": doc_id}

        # Embeddings — claim before dispatch for the same fan-out protection.
        db_emb = get_db_session()
        try:
            if claim_stage_for_dispatch(doc_id, PipelineStage.EMBEDDINGS, db_emb):
                try:
                    from app.tasks.generate_embedding import generate_embedding_task

                    generate_embedding_task.delay(doc_id)
                except Exception as e:
                    logger.error(
                        "Doc #%d: generate_embedding dispatch raised — marking stage failed: %s",
                        doc_id,
                        e,
                        exc_info=True,
                    )
                    mark_failed_with_cascade(
                        doc_id,
                        PipelineStage.EMBEDDINGS,
                        db,
                        error=f"dispatch failed: {e}",
                    )
            else:
                logger.debug(
                    "Doc #%d: EMBEDDINGS already claimed — skipping dispatch", doc_id
                )
        finally:
            db_emb.close()

        return {"status": "success", "doc_id": doc_id}
    finally:
        db.close()


_METADATA_MAX_RETRIES = 3
_METADATA_BACKOFF = [10, 30, 60]  # seconds between attempts 1→2, 2→3, and final


def _retry_or_fail_after_gate_timeout(task, doc_id: int, exc: ModelGateTimeout) -> None:
    """Re-queue metadata_task after a model-gate timeout, or fail the doc once
    the retry budget is spent (then return so the caller carries on through its
    normal post-failure flow: batch claim, case brief).

    Raises ``celery.exceptions.Retry`` (via ``task.retry``) while retries remain.
    """
    from app.services.pipeline_status import mark_failed_with_cascade, schedule_retry

    db = get_db_session()
    try:
        if task.request.retries < task.max_retries:
            logger.warning(
                "Doc #%d: METADATA could not get the model slot (%s) — "
                "retry %d/%d in %ds",
                doc_id,
                exc,
                task.request.retries + 1,
                task.max_retries,
                _GATE_RETRY_COUNTDOWN,
            )
            schedule_retry(
                doc_id,
                PipelineStage.METADATA,
                db,
                error=str(exc),
                attempt=task.request.retries + 1,
                max_attempts=task.max_retries,
                countdown=_GATE_RETRY_COUNTDOWN,
            )
        else:
            logger.warning(
                "Doc #%d: METADATA gave up waiting for the model slot after %d retries",
                doc_id,
                task.max_retries,
            )
            mark_failed_with_cascade(
                doc_id,
                PipelineStage.METADATA,
                db,
                error=f"model busy after {task.max_retries} retries: {exc}",
                cascade=METADATA_FAILURE_CASCADE,
            )
            return
    finally:
        db.close()
    raise task.retry(exc=exc, countdown=_GATE_RETRY_COUNTDOWN) from exc


def _run_phase1_summary(doc_id: int) -> None:
    """Run Phase 1 metadata extraction with transient-error retry.

    Retries up to _METADATA_MAX_RETRIES times on network/timeout errors.
    Non-transient exceptions mark the stage failed immediately (no retry).

    Status transitions on transient errors:
        running → retrying (during sleep) → running (next attempt) → … → completed | failed
    """
    from app.models.enums import PipelineStage
    from app.services.ai_summary import _summarize_document_sync
    from app.services.pipeline_status import (
        mark_completed,
        mark_failed,
        mark_failed_with_cascade,
        mark_started,
        schedule_retry,
    )

    db2 = get_db_session()
    try:
        mark_started(doc_id, PipelineStage.METADATA, db2)
    finally:
        db2.close()

    last_error: Exception | None = None
    try:
        for attempt in range(_METADATA_MAX_RETRIES):
            db2 = get_db_session()
            try:
                _summarize_document_sync(doc_id, db2)
                mark_completed(doc_id, PipelineStage.METADATA, db2)
                return
            except ModelGateTimeout:
                # The model was never called; the caller re-queues the whole
                # task instead of failing the doc (see metadata_task).
                db2.rollback()
                raise
            except _TRANSIENT_AI_ERRORS + (
                SA_OperationalError,
                httpx.HTTPStatusError,
            ) as e:
                permanent = (
                    isinstance(e, SA_OperationalError) and not is_db_locked(e)
                ) or (
                    isinstance(e, httpx.HTTPStatusError)
                    and not is_transient_ai_http_error(e)
                )
                if permanent:
                    db2.rollback()
                    mark_failed_with_cascade(
                        doc_id,
                        PipelineStage.METADATA,
                        db2,
                        error=str(e),
                        cascade=METADATA_FAILURE_CASCADE,
                    )
                    logger.warning(f"Phase 1 summary failed for doc {doc_id}: {e}")
                    return
                last_error = e
                db2.close()
                if attempt < _METADATA_MAX_RETRIES - 1:
                    wait = _METADATA_BACKOFF[attempt]
                    logger.info(
                        f"Doc {doc_id}: METADATA transient error (attempt {attempt + 1}/{_METADATA_MAX_RETRIES}), "
                        f"retrying in {wait}s: {e}"
                    )
                    # Flip to RETRYING so the UI shows "Retrying METADATA (N/M) in Ws"
                    # instead of a silent spin.
                    db_retry = get_db_session()
                    try:
                        schedule_retry(
                            doc_id,
                            PipelineStage.METADATA,
                            db_retry,
                            error=str(e),
                            attempt=attempt + 1,
                            max_attempts=_METADATA_MAX_RETRIES,
                            countdown=wait,
                        )
                    finally:
                        db_retry.close()
                    time.sleep(wait)
                    # Flip back to RUNNING for the next attempt — keeps the
                    # Celery and in-process retry semantics consistent.
                    db_start = get_db_session()
                    try:
                        mark_started(doc_id, PipelineStage.METADATA, db_start)
                    finally:
                        db_start.close()
            except Exception as e:
                db2.rollback()
                mark_failed_with_cascade(
                    doc_id,
                    PipelineStage.METADATA,
                    db2,
                    error=str(e),
                    cascade=METADATA_FAILURE_CASCADE,
                )
                logger.warning(f"Phase 1 summary failed for doc {doc_id}: {e}")
                return
            finally:
                db2.close()
    except BaseException as e:
        # This outer handler is normally only reached by a genuine
        # BaseException-not-Exception (worker killed by signal) — every
        # per-attempt Exception is already handled and returns above,
        # inside the loop. SoftTimeLimitExceeded breaks that assumption:
        # it can fire during the time.sleep() backoff between retry
        # attempts, which sits outside any per-attempt try/except, so it
        # lands here despite being an Exception subclass. Unlike a true
        # worker-kill (left uncascaded so recovery can resume it — that's
        # a crash, not a content failure), a soft time limit means this
        # genuinely took too long: cascade it like any other terminal
        # METADATA failure so CLAIMS doesn't stay stuck and the case brief
        # isn't blocked.
        if isinstance(e, SoftTimeLimitExceeded):
            _db = get_db_session()
            try:
                mark_failed_with_cascade(
                    doc_id,
                    PipelineStage.METADATA,
                    _db,
                    error=f"soft time limit exceeded: {e}",
                    cascade=METADATA_FAILURE_CASCADE,
                )
            except Exception:
                pass
            finally:
                _db.close()
            # Don't re-raise: the whole point of catching this is to finish
            # gracefully. Returning lets metadata_task proceed through its
            # normal post-failure flow (attempt the batch claim, trigger the
            # case brief) instead of aborting via an uncaught exception —
            # unlike the true-crash branch below, this isn't a worker dying,
            # it's this doc's METADATA genuinely taking too long.
            return
        if not isinstance(e, Exception):
            _db = get_db_session()
            try:
                mark_failed(
                    doc_id,
                    PipelineStage.METADATA,
                    _db,
                    error=f"task aborted: {type(e).__name__}",
                )
            except Exception:
                pass
            finally:
                _db.close()
        raise

    # All transient retries exhausted
    db2 = get_db_session()
    try:
        mark_failed_with_cascade(
            doc_id,
            PipelineStage.METADATA,
            db2,
            error=f"timeout after {_METADATA_MAX_RETRIES} attempts: {last_error}",
            cascade=METADATA_FAILURE_CASCADE,
        )
        logger.warning(
            f"Doc {doc_id}: METADATA failed after {_METADATA_MAX_RETRIES} attempts: {last_error}"
        )
    finally:
        db2.close()

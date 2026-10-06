"""The triage inbox: feed, bundle actions, case routing and sub-groups.

Every by-id route resolves its target with an explicit owner check: the
legacy router-level guard only read ids from the path/query/form, which a
JSON body never satisfies.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.v1.errors import ApiError
from app.core.rate_limit import limiter
from app.dependencies import get_current_user, get_db
from app.models.database import Case, Document, IngestBatch, Proceeding, User
from app.models.enums import PipelineState, StageStatus
from app.repositories.case import CaseRepository
from app.schemas.triage import (
    BatchAssign,
    BatchKeys,
    BatchResult,
    BundlePipeline,
    BundleRetry,
    ConfirmedCase,
    CoverUpdate,
    GroupOrder,
    GroupRename,
    GroupTarget,
    PickerCase,
    PickerOption,
    PickerProceeding,
    RetryAllResult,
    SlicingQueueItem,
    TitleUpdate,
    TriageActionDate,
    TriageBundle,
    TriageCaseSuggestion,
    TriageConfirm,
    TriageConfirmResult,
    TriageDocument,
    TriageProceeding,
    TriageStats,
    TriageSubGroup,
    TriageView,
)
from app.services import access_service
from app.services.pipeline_status import retry_on_db_locked, stages_dict
from app.services.triage_bundles import (
    BundleView,
    _bundle_pipeline_label,
    get_slicing_queue,
    get_triage_bundles,
    get_triage_filter_options,
)
from app.services.triage_confirmation import (
    cleanup_orphaned_drafts,
    confirm_bundle,
    confirm_document,
    get_bundle_suggestion,
    reset_and_reenrich,
)
from app.services.triage_dismissal import delete_bundle, dismiss_bundle
from app.services.triage_retry import dispatch_batch_retry, reset_batch_for_retry
from app.services.triage_view import failed_doc_summary, stats_for_chips

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/triage", tags=["triage"])


# --- Ownership ---------------------------------------------------------------


def owned_batch(
    batch_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> IngestBatch:
    batch = db.get(IngestBatch, batch_id)
    if batch is None or batch.owner_id != user.id:
        raise ApiError(404, "not_found", "Bundle not found.")
    return batch


def owned_document(
    doc_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Document:
    doc = db.get(Document, doc_id)
    if doc is None or doc.owner_id != user.id:
        raise ApiError(404, "not_found", "Document not found.")
    return doc


def _parse_key(key: str) -> tuple[int | None, int | None]:
    kind, _, raw = key.partition("-")
    if not raw.isdigit():
        return None, None
    if kind == "batch":
        return int(raw), None
    if kind == "loose":
        return None, int(raw)
    return None, None


def _key_owned(
    db: Session, user: User, batch_id: int | None, doc_id: int | None
) -> bool:
    if batch_id is not None:
        batch = db.get(IngestBatch, batch_id)
        return batch is not None and batch.owner_id == user.id
    if doc_id is not None:
        doc = db.get(Document, doc_id)
        return doc is not None and doc.owner_id == user.id
    return False


# --- DTO mapping -------------------------------------------------------------


def _failed_error(bundle: BundleView) -> str | None:
    for doc in bundle.documents:
        if doc.pipeline_state == PipelineState.FAILED:
            for rec in stages_dict(doc).values():
                if isinstance(rec, dict) and rec.get("status") == StageStatus.FAILED:
                    return rec.get("error") or "Pipeline failed"
            return "Pipeline failed"
    return None


def bundle_dto(bundle: BundleView) -> TriageBundle:
    summary = bundle.pipeline_summary
    counts = {state: summary.get(state.value, 0) for state in PipelineState}
    depth_by_doc = {d.id: depth for group in bundle.parent_groups for depth, d in group}
    sub_bundles = bundle.sub_bundles
    lead = sub_bundles[0].lead_doc if sub_bundles and sub_bundles[0].lead_doc else None
    return TriageBundle(
        key=bundle.key,
        batch_id=bundle.batch_id,
        is_synthetic=bundle.is_synthetic,
        source_type=bundle.source_type,
        subject=bundle.subject,
        sender_email=bundle.sender_email,
        received_at=bundle.received_at,
        status=bundle.mock_status,  # type: ignore[arg-type]
        pipeline_filter=_bundle_pipeline_label(bundle),  # type: ignore[arg-type]
        pipeline=BundlePipeline(
            total=summary["total"],
            counts=counts,
            active_label=bundle.pipeline_active_label,
            failed_error=_failed_error(bundle),
        ),
        confirmed_case_id=bundle.confirmed_case_id,
        suggestion=TriageCaseSuggestion(
            case_id=bundle.suggested_case_id,
            title=bundle.suggested_case_title,
            is_draft=bundle.suggested_case_is_draft,
            exists=bundle.suggested_case_exists,
        )
        if bundle.suggested_case_id
        else None,
        proceeding=TriageProceeding(
            id=bundle.proceeding.id,
            az_court=bundle.proceeding.az_court,
            court_name=bundle.proceeding.court_name,
            court_level=bundle.proceeding.court_level,
        )
        if bundle.proceeding
        else None,
        doc_count=bundle.doc_count,
        total_pages=bundle.total_pages,
        originator_types=list(
            dict.fromkeys(d.originator_type for d in bundle.documents)
        ),
        action_dates=[
            TriageActionDate(title=a.title, due_date=a.due_date)
            for a in bundle.action_items[:3]
        ],
        has_manual_groups=bool(bundle.sub_groups),
        has_unconfirmed_metadata=bundle.has_unconfirmed_metadata,
        unresolved_review_count=bundle.unresolved_review_count,
        to_confirm_count=bundle.to_confirm_count,
        lead_doc_id=lead.id
        if lead
        else (bundle.documents[0].id if bundle.documents else None),
        documents=[
            TriageDocument(
                id=d.id,
                title=d.title,
                role=d.role,
                depth=depth_by_doc.get(d.id, 0),
                originator_type=d.originator_type,
                significance_tier=d.significance_tier,
                pipeline_state=d.pipeline_state,
                needs_review=bool(d.needs_review),
                review_reasons=list(d.review_reasons or []),
                page_count=d.page_count or 0,
                is_proof=d.id in bundle.proof_doc_ids,
                sub_group_id=d.sub_group_id,
            )
            for d in bundle.documents
        ],
        sub_groups=[
            TriageSubGroup(
                id=sb.id,
                sub_group_id=sb.sub_group_id,
                label=sb.label,
                lead_doc_id=sb.lead_doc.id if sb.lead_doc else None,
                suggested_case_id=sb.suggested_case_id,
                suggested_case_title=sb.suggested_case_title,
                case_confidence=sb.field_confidence_case,
                doc_ids=[d.id for _, d in sb.docs],
            )
            for sb in sub_bundles
        ],
    )


def _bundle_by_key(db: Session, user: User, key: str) -> TriageBundle | None:
    bundles = get_triage_bundles(db, owner_id=user.id)
    found = next((b for b in bundles if b.key == key), None)
    return bundle_dto(found) if found else None


def _stats(db: Session, user: User, bundles: list[BundleView]) -> TriageStats:
    editable = access_service.editable_case_ids(db, user)
    drafts_q = db.query(Case).filter(Case.is_draft.is_(True))
    if editable is not None:
        drafts_q = drafts_q.filter(Case.id.in_(editable))
    drafts_pending = drafts_q.count()
    first_draft_doc_id = None
    if drafts_pending:
        q = (
            db.query(Document.id)
            .join(Case, Case.id == Document.case_id)
            .filter(Case.is_draft.is_(True))
        )
        if editable is not None:
            q = q.filter(Case.id.in_(editable))
        row = q.order_by(Document.id.asc()).first()
        first_draft_doc_id = row[0] if row else None
    failed_count, first_failed = failed_doc_summary(bundles)
    chips = stats_for_chips(bundles)
    return TriageStats(
        pending=chips["pending"],
        needs_classification=chips["needs_classification"],
        needs_review=chips["needs_review"],
        stuck=chips["stuck"],
        processing=chips["processing"],
        failed_docs=failed_count,
        first_failed_doc_id=first_failed,
        drafts_pending=drafts_pending,
        first_draft_doc_id=first_draft_doc_id,
    )


# --- Feed --------------------------------------------------------------------


@router.get("", response_model=TriageView)
def triage(
    sort: str = Query("received", pattern="^(received|docs|status)$"),
    dir: str = Query("desc", pattern="^(asc|desc)$"),
    case_id: list[str] = Query(default=[]),
    proceeding_id: list[str] = Query(default=[]),
    pipeline_filter: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bundles = get_triage_bundles(
        db,
        limit=500,
        sort=sort,
        direction=dir,
        case_ids=case_id,
        proceeding_ids=proceeding_id,
        pipeline_filters=pipeline_filter,
        owner_id=user.id,
    )
    options = get_triage_filter_options(db, owner_id=user.id)
    editable = access_service.editable_case_ids(db, user)
    proceedings_q = db.query(Proceeding).order_by(Proceeding.court_name.asc())
    if editable is not None:
        proceedings_q = proceedings_q.filter(Proceeding.case_id.in_(editable))
    return TriageView(
        bundles=[bundle_dto(b) for b in bundles],
        stats=_stats(db, user, bundles),
        filter_options={
            "cases": [
                PickerOption(value=v, label=text) for v, text in options["case_options"]
            ],
            "proceedings": [
                PickerOption(value=v, label=text)
                for v, text in options["proceeding_options"]
            ],
            "pipeline": [
                PickerOption(value=v, label=text)
                for v, text in options["pipeline_options"]
            ],
        },
        cases=[
            PickerCase(id=c.id, title=c.title)
            for c in CaseRepository(db).list_for_picker(owner_id=user.id)
        ],
        proceedings=[
            PickerProceeding(
                id=p.id,
                case_id=p.case_id,
                label=" · ".join(x for x in (p.court_name, p.az_court) if x),
            )
            for p in proceedings_q.all()
        ],
        slicing_queue=[
            SlicingQueueItem(
                batch_id=b.id,
                subject=b.subject,
                page_count=(b.meta or {}).get("slicing", {}).get("page_count"),
                status=(b.meta or {}).get("slicing", {}).get("status", "preparing"),
            )
            for b in get_slicing_queue(db, owner_id=user.id)
        ],
    )


@router.get("/bundles/{batch_id}", response_model=TriageBundle)
def bundle(
    batch: IngestBatch = Depends(owned_batch),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    found = _bundle_by_key(db, user, f"batch-{batch.id}")
    if found is None:
        raise ApiError(404, "not_found", "Bundle is no longer in triage.")
    return found


# --- Dismiss / delete / retry ------------------------------------------------


@router.post("/bundles/{batch_id}/dismiss", status_code=204, response_class=Response)
def dismiss(batch: IngestBatch = Depends(owned_batch), db: Session = Depends(get_db)):
    dismiss_bundle(db, batch_id=batch.id)


@router.post("/documents/{doc_id}/dismiss", status_code=204, response_class=Response)
def dismiss_loose(
    doc: Document = Depends(owned_document), db: Session = Depends(get_db)
):
    dismiss_bundle(db, doc_id=doc.id)


def _delete(db: Session, **target: int) -> None:
    try:
        ok = delete_bundle(db, **target)
    except ValueError as exc:
        raise ApiError(409, "in_flight", str(exc)) from exc
    if not ok:
        raise ApiError(404, "not_found", "Bundle not found.")


@router.delete("/bundles/{batch_id}", status_code=204, response_class=Response)
def delete(batch: IngestBatch = Depends(owned_batch), db: Session = Depends(get_db)):
    _delete(db, batch_id=batch.id)


@router.delete("/documents/{doc_id}", status_code=204, response_class=Response)
def delete_loose(
    doc: Document = Depends(owned_document), db: Session = Depends(get_db)
):
    _delete(db, doc_id=doc.id)


@router.post("/bundles/{batch_id}/retry", response_model=TriageBundle)
@limiter.limit("20/minute")
def retry_bundle(
    request: Request,
    body: BundleRetry,
    batch: IngestBatch = Depends(owned_batch),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Re-run the AI stages for every document in the bundle (``full`` re-extracts too)."""

    def _do_reset():
        result = reset_batch_for_retry(batch, db, full=body.full)
        if result == -1:
            return -1, None
        items, fallback = result
        db.commit()
        return items, fallback

    try:
        items, fallback = retry_on_db_locked(_do_reset, db)
    except OperationalError as exc:
        raise ApiError(
            409, "worker_busy", "Worker busy — try again in a moment."
        ) from exc
    if items == -1:
        raise ApiError(409, "in_flight", "A pipeline stage is actively running.")
    dispatch_batch_retry(items, batch_fallback=fallback, batch_id=batch.id, db=db)
    found = _bundle_by_key(db, user, f"batch-{batch.id}")
    if found is None:
        raise ApiError(404, "not_found", "Bundle is no longer in triage.")
    return found


@router.post("/retry-all", response_model=RetryAllResult)
@limiter.limit("5/minute")
def retry_all(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    bundles = get_triage_bundles(db, limit=500, enrich=False, owner_id=user.id)
    batch_ids = {b.batch_id for b in bundles if b.batch_id is not None}
    retried = 0
    for batch in db.query(IngestBatch).filter(IngestBatch.id.in_(batch_ids)).all():

        def _do_reset(b=batch):
            res = reset_batch_for_retry(b, db, full=False)
            if res == -1:
                return -1
            items, fallback = res
            db.commit()
            return items, fallback

        try:
            result = retry_on_db_locked(_do_reset, db)
        except OperationalError:
            logger.warning("retry-all: batch %s still locked; skipping", batch.id)
            continue
        if result == -1:
            continue
        items, fallback = result
        dispatch_batch_retry(items, batch_fallback=fallback, batch_id=batch.id, db=db)
        retried += 1
    return RetryAllResult(retried=retried)


# --- Routing to a case ---------------------------------------------------------


def _require_editable_target(db: Session, user: User, case_id: str) -> Case | None:
    if case_id == "_TRIAGE":
        return None
    case = db.get(Case, case_id)
    if case is None:
        raise ApiError(404, "not_found", f"Case {case_id} not found.")
    if not access_service.can_edit_case(db, user, case):
        raise ApiError(403, "forbidden", "You cannot assign to that case.")
    return case


def _check_proceeding(db: Session, proceeding_id: int | None, case_id: str) -> None:
    if proceeding_id is None:
        return
    proc = db.get(Proceeding, proceeding_id)
    if proc is None or proc.case_id != case_id:
        raise ApiError(
            422, "proceeding_mismatch", "proceeding_id does not belong to case_id."
        )


def _resolve_case(
    db: Session, user: User, body: TriageConfirm | BatchAssign
) -> tuple[str, Literal["created", "assigned"]]:
    """Return (case_id, action) creating the case when ``new_case_id`` is set."""
    from app.services.case_service import get_or_create_case_from_reference

    if body.new_case_id:
        case, _, created = get_or_create_case_from_reference(
            db,
            internal_id=body.new_case_id.strip(),
            batch_subject=(body.new_case_title or "").strip() or None,
            is_draft=False,
            owner_id=user.id,
        )
        db.flush()
        return case.id, "created" if created else "assigned"
    if not body.case_id:
        raise ApiError(422, "validation_error", "case_id is required.")
    return body.case_id, "assigned"


def _route_batch(
    db: Session, batch_id: int, case_id: str, proceeding_id: int | None, finalize: bool
) -> None:
    pre_triage = (
        db.query(Document)
        .filter(Document.ingest_batch_id == batch_id, Document.case_id == "_TRIAGE")
        .all()
    )
    batch = confirm_bundle(
        db, batch_id, case_id=case_id, proceeding_id=proceeding_id, finalize=finalize
    )
    if batch is None:
        raise ApiError(404, "not_found", "Bundle not found.")
    if case_id != "_TRIAGE" and pre_triage:
        for d in pre_triage:
            db.refresh(d)
        reset_and_reenrich(db, pre_triage)


def _route_document(
    db: Session, doc_id: int, case_id: str, proceeding_id: int | None, finalize: bool
) -> None:
    pre_case = db.query(Document.case_id).filter(Document.id == doc_id).scalar()
    doc = confirm_document(db, doc_id, case_id=case_id, finalize=finalize)
    if doc is None:
        raise ApiError(404, "not_found", "Document not found.")
    if proceeding_id is not None:
        doc.proceeding_id = proceeding_id
        proc = db.get(Proceeding, proceeding_id)
        if proc and proc.is_draft:
            proc.is_draft = False
        db.commit()
        db.refresh(doc)
    if (not pre_case or pre_case == "_TRIAGE") and case_id != "_TRIAGE":
        reset_and_reenrich(db, [doc])
    # Moving the last document off an AI draft case leaves it orphaned.
    cleanup_orphaned_drafts(db)


def _next_doc_id(bundles: list[BundleView]) -> int | None:
    for b in bundles:
        for d in b.documents:
            if d.needs_review or d.case_id == "_TRIAGE":
                return d.id
    return None


@router.post("/confirm", response_model=TriageConfirmResult)
@limiter.limit("30/minute")
def confirm_bundle_route(
    request: Request,
    body: TriageConfirm,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if (body.batch_id is None) == (body.doc_id is None):
        raise ApiError(
            422, "validation_error", "Pass exactly one of batch_id or doc_id."
        )
    if not _key_owned(db, user, body.batch_id, body.doc_id):
        raise ApiError(404, "not_found", "Bundle not found.")
    case_id, action = _resolve_case(db, user, body)
    case = _require_editable_target(db, user, case_id)
    _check_proceeding(db, body.proceeding_id, case_id)
    finalize = body.action == "confirm_bundle"
    if body.batch_id is not None:
        key = f"batch-{body.batch_id}"
        _route_batch(db, body.batch_id, case_id, body.proceeding_id, finalize)
    else:
        key = f"loose-{body.doc_id}"
        _route_document(db, body.doc_id or 0, case_id, body.proceeding_id, finalize)

    bundles = get_triage_bundles(db, owner_id=user.id)
    updated = next((b for b in bundles if b.key == key), None)
    if updated is not None:
        next_id = updated.documents[0].id if updated.documents else None
    else:
        next_id = _next_doc_id(bundles)
    return TriageConfirmResult(
        bundle=bundle_dto(updated) if updated else None,
        next_doc_id=next_id,
        case=ConfirmedCase(
            id=case_id, title=case.title if case else case_id, action=action
        ),  # type: ignore[arg-type]
    )


@router.post("/batch/confirm", response_model=BatchResult)
@limiter.limit("30/minute")
def batch_confirm(
    request: Request,
    body: BatchKeys,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Confirm each selected bundle to its own AI-suggested case; skip the rest."""
    confirmed_keys: list[str] = []
    skipped = 0
    for key in body.keys:
        batch_id, doc_id = _parse_key(key)
        if not _key_owned(db, user, batch_id, doc_id):
            skipped += 1
            continue
        case_id, proceeding_id = get_bundle_suggestion(
            db, batch_id=batch_id, doc_id=doc_id
        )
        target = db.get(Case, case_id) if case_id else None
        if (
            case_id is None
            or target is None
            or not access_service.can_edit_case(db, user, target)
        ):
            skipped += 1
            continue
        proc = db.get(Proceeding, proceeding_id) if proceeding_id else None
        if proc is None or proc.case_id != case_id:
            proceeding_id = None
        if batch_id is not None:
            _route_batch(db, batch_id, case_id, proceeding_id, True)
        else:
            _route_document(db, doc_id or 0, case_id, proceeding_id, True)
        confirmed_keys.append(key)
    return _batch_result(db, user, confirmed_keys, skipped)


@router.post("/batch/assign", response_model=BatchResult)
@limiter.limit("30/minute")
def batch_assign(
    request: Request,
    body: BatchAssign,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Assign every selected bundle to one case (stays in triage for review)."""
    case_id, _ = _resolve_case(db, user, body)
    _require_editable_target(db, user, case_id)
    _check_proceeding(db, body.proceeding_id, case_id)
    done: list[str] = []
    skipped = 0
    for key in body.keys:
        batch_id, doc_id = _parse_key(key)
        if not _key_owned(db, user, batch_id, doc_id):
            skipped += 1
            continue
        if batch_id is not None:
            _route_batch(db, batch_id, case_id, body.proceeding_id, False)
        else:
            _route_document(db, doc_id or 0, case_id, body.proceeding_id, False)
        done.append(key)
    return _batch_result(db, user, done, skipped)


def _batch_result(
    db: Session, user: User, keys: list[str], skipped: int
) -> BatchResult:
    bundles = {b.key: b for b in get_triage_bundles(db, owner_id=user.id)}
    return BatchResult(
        confirmed=len(keys),
        skipped=skipped,
        bundles=[bundle_dto(bundles[k]) for k in keys if k in bundles],
        removed_keys=[k for k in keys if k not in bundles],
    )


# --- Document title and sub-groups ------------------------------------------


@router.put("/documents/{doc_id}/title", status_code=204, response_class=Response)
def set_title(
    body: TitleUpdate,
    doc: Document = Depends(owned_document),
    db: Session = Depends(get_db),
):
    doc.title = body.title.strip()
    db.commit()


def _after_groups(db: Session, user: User, batch: IngestBatch) -> TriageBundle:
    found = _bundle_by_key(db, user, f"batch-{batch.id}")
    if found is None:
        raise ApiError(404, "not_found", "Bundle is no longer in triage.")
    return found


def _groups_call(db: Session, fn, *args, **kwargs):
    """Run a sub-group service call (they only flush) and commit it."""
    try:
        result = fn(*args, **kwargs)
    except ValueError as exc:
        db.rollback()
        raise ApiError(422, "validation_error", str(exc)) from exc
    db.commit()
    return result


@router.put("/bundles/{batch_id}/cover", response_model=TriageBundle)
def set_cover(
    body: CoverUpdate,
    batch: IngestBatch = Depends(owned_batch),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.triage_subgroups import set_cover_letter

    _groups_call(db, set_cover_letter, db, body.doc_id, batch.id)
    return _after_groups(db, user, batch)


@router.post("/bundles/{batch_id}/groups", response_model=TriageBundle)
def new_group(
    batch: IngestBatch = Depends(owned_batch),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.triage_subgroups import create_sub_group

    _groups_call(db, create_sub_group, db, batch.id)
    return _after_groups(db, user, batch)


@router.put("/bundles/{batch_id}/groups/{sub_group_id}", response_model=TriageBundle)
def rename_group(
    sub_group_id: int,
    body: GroupRename,
    batch: IngestBatch = Depends(owned_batch),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.triage_subgroups import rename_sub_group

    _groups_call(
        db,
        rename_sub_group,
        db,
        sub_group_id or None,
        batch.id,
        body.label,
        body.lead_doc_id,
    )
    return _after_groups(db, user, batch)


@router.delete("/bundles/{batch_id}/groups/{sub_group_id}", response_model=TriageBundle)
def delete_group(
    sub_group_id: int,
    body: GroupTarget | None = None,
    batch: IngestBatch = Depends(owned_batch),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.triage_subgroups import delete_sub_group

    _groups_call(
        db,
        delete_sub_group,
        db,
        sub_group_id or None,
        batch.id,
        body.lead_doc_id if body else None,
    )
    return _after_groups(db, user, batch)


@router.put(
    "/bundles/{batch_id}/groups/{sub_group_id}/order", response_model=TriageBundle
)
def reorder_group(
    sub_group_id: int,
    body: GroupOrder,
    batch: IngestBatch = Depends(owned_batch),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.triage_subgroups import reorder_documents

    _groups_call(
        db,
        reorder_documents,
        db,
        batch.id,
        body.doc_ids,
        sub_group_id or None,
        body.lead_doc_id,
    )
    return _after_groups(db, user, batch)


@router.post("/bundles/{batch_id}/groups/reset", response_model=TriageBundle)
def reset_groups(
    batch: IngestBatch = Depends(owned_batch),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.triage_subgroups import reset_sub_groups

    _groups_call(db, reset_sub_groups, db, batch.id)
    return _after_groups(db, user, batch)

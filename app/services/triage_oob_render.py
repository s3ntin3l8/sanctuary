"""OOB render helpers for the triage feed.

Extracted from `app.services.triage_view` to break a circular import: the
triage_service module imports back into triage_view via lazy properties, so
these render helpers (which need FastAPI/templates/Session) had to live below
all code to avoid ImportError at parse time. They now live here instead.

Callers: app/api/triage.py, app/api/documents.py, app/api/cases.py.
"""

from datetime import datetime

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import templates
from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS
from app.models.enums import (
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    UserReactionType,
)


def render_bundle_group_oob(request: Request, bundle, triage_service) -> str:
    """Render one bundle row as an OOB swap fragment.

    Replaces the entire bundle row (and its inline expand) in-place without
    touching the rest of the feed — preserves scroll position and Alpine state.
    """
    reactions_by_doc = triage_service.get_reactions_by_doc_ids(
        [doc.id for doc in bundle.documents]
    )
    return templates.get_template("partials/triage_row.html").render(
        {
            "request": request,
            "bundle": bundle,
            "reactions_by_doc": reactions_by_doc,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
            "ORIGINATOR_COLORS": ORIGINATOR_COLORS,
            "OriginatorType": OriginatorType,
            "UserReactionType": UserReactionType,
            "hx_swap_oob": True,
        }
    )


def render_triage_feed_oob(request: Request, triage_service, db: Session) -> str:
    """Renders the full triage feed as an OOB swap (used by bundle confirms)."""
    from app.models.database import Proceeding

    # Preserve active filters from the request URL
    case_ids = request.query_params.getlist("case_id")
    proceeding_ids = request.query_params.getlist("proceeding_id")
    pipeline_filters = request.query_params.getlist("pipeline_filter")

    filter_options = triage_service.get_triage_filter_options()
    bundles = triage_service.get_triage_bundles(
        case_ids=case_ids or None,
        proceeding_ids=proceeding_ids or None,
        pipeline_filters=pipeline_filters or None,
    )
    all_doc_ids = [doc.id for bundle in bundles for doc in bundle.documents]
    reactions_by_doc = triage_service.get_reactions_by_doc_ids(all_doc_ids)
    from app.repositories.case import CaseRepository

    all_cases = CaseRepository(db).list_for_picker()
    proceedings = db.query(Proceeding).order_by(Proceeding.court_name.asc()).all()

    return templates.get_template("partials/triage_feed.html").render(
        {
            "request": request,
            "bundles": bundles,
            "cases": all_cases,
            "proceedings": proceedings,
            "reactions_by_doc": reactions_by_doc,
            "originator_colors": ORIGINATOR_COLORS,
            "originator_icons": ORIGINATOR_ICONS,
            "OriginatorType": OriginatorType,
            "UserReactionType": UserReactionType,
            "as_oob": True,
            "case_ids": case_ids,
            "proceeding_ids": proceeding_ids,
            "pipeline_filters": pipeline_filters,
            "case_options": filter_options["case_options"],
            "proceeding_options": filter_options["proceeding_options"],
            "pipeline_options": filter_options["pipeline_options"],
        }
    )


def render_row_targeted_oob(
    request: Request,
    doc,
    triage_service,
    db: Session,
    allow_delete: bool = True,
) -> str:
    """Targeted OOB for a single doc confirm: rebuilds the doc's bundle row.

    Avoids the full feed replacement that causes flicker, scroll reset, and
    Alpine state loss. Returns a delete swap on the bundle row if the document
    was the last in the bundle and should leave triage, unless
    allow_delete=False (used by the passive 4s polling probe so the row stays
    visible until the user explicitly acts or refreshes the page).
    """
    from app.services.triage_service import BundleView

    in_triage_via_case = doc.case_id == "_TRIAGE" or doc.needs_review
    in_triage_via_batch = False
    if doc.ingest_batch_id:
        batch = doc.ingest_batch
        if batch and batch.status not in (
            IngestBatchStatus.COMPLETED,
            IngestBatchStatus.AWAITING_SLICING,
        ):
            in_triage_via_batch = True

    bundle_key = (
        f"batch-{doc.ingest_batch_id}" if doc.ingest_batch_id else f"loose-{doc.id}"
    )
    should_delete = not in_triage_via_case and not in_triage_via_batch
    if should_delete and allow_delete:
        return (
            f'<div id="triage-row-{bundle_key}" hx-swap-oob="delete"></div>'
            f'<div id="triage-row-expanded-{bundle_key}" hx-swap-oob="delete"></div>'
        )

    bundle = None
    if doc.ingest_batch_id:
        bundle = triage_service.get_bundle_by_batch_id(doc.ingest_batch_id)
        if bundle and not in_triage_via_batch and allow_delete:
            bundle.documents = [
                d for d in bundle.documents if d.case_id == "_TRIAGE" or d.needs_review
            ]
            triage_service.enrich_bundle(bundle)
    else:
        bundle = BundleView(
            key=f"loose-{doc.id}",
            batch_id=None,
            source_type=IngestBatchSourceType.MANUAL,
            subject=doc.title,
            sender_email=None,
            received_at=doc.ingest_date or datetime.now(),
            confirmed_case_id=doc.case_id if doc.case_id != "_TRIAGE" else None,
            proceeding=doc.proceeding,
            documents=[doc],
        )
        triage_service.enrich_bundle(bundle)

    if not bundle or not any(d.id == doc.id for d in bundle.documents):
        if not allow_delete:
            return ""
        return (
            f'<div id="triage-row-{bundle_key}" hx-swap-oob="delete"></div>'
            f'<div id="triage-row-expanded-{bundle_key}" hx-swap-oob="delete"></div>'
        )

    return render_bundle_group_oob(request, bundle, triage_service)


def render_sidebar_badges_oob(db: Session) -> str:
    """Render global sidebar badges (triage, notifications) as OOB swaps."""
    from app.helpers import _build_notifications, build_sidebar_counts

    counts = build_sidebar_counts(db)
    notif_data = _build_notifications(db)
    notif_count = notif_data["notification_count"]

    triage_badge_inner = ""
    if counts["triage_count"] > 0:
        triage_badge_inner = (
            f'<span class="absolute -top-1 -right-1 flex items-center justify-center min-w-[16px] h-4 px-1 bg-error text-surface text-[9px] font-bold rounded-full border-2 border-surface-container-low">'
            f"{counts['triage_count']}</span>"
        )
    triage_oob = f'<div id="sidebar-triage-badge-container" hx-swap-oob="true">{triage_badge_inner}</div>'

    notif_badge_inner = ""
    if notif_count > 0:
        notif_badge_inner = (
            f'<span class="absolute -top-1 -right-1 flex items-center justify-center min-w-[16px] h-4 px-1 bg-error text-surface text-[9px] font-bold rounded-full border-2 border-surface-container-low">'
            f"{notif_count}</span>"
        )
    notif_oob = f'<div id="sidebar-notifications-badge-container" hx-swap-oob="true">{notif_badge_inner}</div>'

    return triage_oob + notif_oob


def render_triage_header_stats_oob(request: Request, triage_service) -> str:
    """Render the redesigned triage header chip stats as an OOB swap.

    Targets `#triage-header-stats` in `partials/triage_filter_chips.html`.
    """
    from app.services.triage_view import stats_for_chips

    bundles = triage_service.get_triage_bundles()
    return templates.get_template("partials/triage_filter_chips.html").render(
        {
            "request": request,
            "header_stats": stats_for_chips(bundles),
            "as_oob": True,
        }
    )


def render_batch_oob(
    request: Request,
    bundle_keys: list[str],
    triage_service,
    db: Session,
) -> str:
    """Build a concatenated OOB response for multiple bundle keys.

    For each key, either swaps the updated row (if the bundle is still in triage)
    or deletes it from the DOM (if it left triage). Always appends badges and
    header-stats OOB fragments.
    """
    parts: list[str] = []
    remaining = triage_service.get_triage_bundles()
    remaining_by_key = {b.key: b for b in remaining}

    for key in bundle_keys:
        if key in remaining_by_key:
            parts.append(
                render_bundle_group_oob(request, remaining_by_key[key], triage_service)
            )
        else:
            parts.append(
                f'<div id="triage-row-{key}" hx-swap-oob="delete"></div>'
                f'<div id="triage-row-expanded-{key}" hx-swap-oob="delete"></div>'
            )

    parts.append(render_sidebar_badges_oob(db))
    parts.append(render_triage_header_stats_oob(request, triage_service))
    return "".join(parts)

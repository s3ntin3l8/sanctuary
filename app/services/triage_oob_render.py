"""OOB render helpers for the triage feed.

Free-function module. Renders out-of-band (OOB) HTMX fragments for the
triage page — bundle rows, feed replacements, header stats, sidebar badges.

Each helper takes a Session directly and calls the appropriate triage_*
module functions (triage_bundles, triage_reactions, etc.).

Callers: app/api/triage/*.py, app/api/documents.py, app/api/cases.py.
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


def render_bundle_group_oob(request: Request, bundle, db: Session) -> str:
    """Render one bundle row as an OOB swap fragment.

    Replaces the entire bundle row (and its inline expand) in-place without
    touching the rest of the feed — preserves scroll position and Alpine state.
    """
    from app.services.triage_reactions import get_reactions_by_doc_ids

    reactions_by_doc = get_reactions_by_doc_ids(
        db, [doc.id for doc in bundle.documents]
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


def render_triage_feed_oob(
    request: Request, db: Session, owner_id: int | None = None
) -> str:
    """Renders the full triage feed as an OOB swap (used by bundle confirms)."""
    from app.models.database import Proceeding
    from app.repositories.case import CaseRepository
    from app.services.triage_bundles import (
        get_triage_bundles,
        get_triage_filter_options,
    )
    from app.services.triage_reactions import get_reactions_by_doc_ids

    # Preserve active filters from the request URL
    case_ids = request.query_params.getlist("case_id")
    proceeding_ids = request.query_params.getlist("proceeding_id")
    pipeline_filters = request.query_params.getlist("pipeline_filter")

    filter_options = get_triage_filter_options(db, owner_id=owner_id)
    bundles = get_triage_bundles(
        db,
        case_ids=case_ids or None,
        proceeding_ids=proceeding_ids or None,
        pipeline_filters=pipeline_filters or None,
        owner_id=owner_id,
    )
    all_doc_ids = [doc.id for bundle in bundles for doc in bundle.documents]
    reactions_by_doc = get_reactions_by_doc_ids(db, all_doc_ids)

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
    from app.services.triage_bundles import (
        BundleView,
        enrich_bundle,
        get_bundle_by_batch_id,
    )

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
        bundle = get_bundle_by_batch_id(db, doc.ingest_batch_id)
        if bundle and not in_triage_via_batch and allow_delete:
            bundle.documents = [
                d for d in bundle.documents if d.case_id == "_TRIAGE" or d.needs_review
            ]
            enrich_bundle(db, bundle)
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
        enrich_bundle(db, bundle)

    if not bundle or not any(d.id == doc.id for d in bundle.documents):
        if not allow_delete:
            return ""
        return (
            f'<div id="triage-row-{bundle_key}" hx-swap-oob="delete"></div>'
            f'<div id="triage-row-expanded-{bundle_key}" hx-swap-oob="delete"></div>'
        )

    return render_bundle_group_oob(request, bundle, db)


def render_sidebar_badges_oob(db: Session, owner_id: int | None = None) -> str:
    """Render global sidebar badges (triage, notifications) as OOB swaps."""
    from app.helpers import _build_notifications, build_sidebar_counts
    from app.models.database import User

    counts = build_sidebar_counts(db, owner_id=owner_id)
    notif_user = db.get(User, owner_id) if owner_id is not None else None
    notif_data = _build_notifications(db, user=notif_user)
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


def render_triage_header_stats_oob(
    request: Request, db: Session, bundles=None, owner_id: int | None = None
) -> str:
    """Render the redesigned triage header chip stats as an OOB swap.

    Targets `#triage-header-stats` in `partials/triage_filter_chips.html`.
    When ``bundles`` is supplied, reuses that list instead of refetching.
    Falls back to an enrich=False fetch (stats only need pipeline state).
    """
    from app.services.triage_bundles import get_triage_bundles
    from app.services.triage_view import stats_for_chips

    if bundles is None:
        bundles = get_triage_bundles(db, enrich=False, owner_id=owner_id)
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
    db: Session,
    owner_id: int | None = None,
) -> str:
    """Build a concatenated OOB response for multiple bundle keys.

    For each key, either swaps the updated row (if the bundle is still in triage)
    or deletes it from the DOM (if it left triage). Always appends badges and
    header-stats OOB fragments. The bundle list is fetched once and reused
    for both the row swaps and the header-stats render.
    """
    from app.services.triage_bundles import get_triage_bundles

    parts: list[str] = []
    remaining = get_triage_bundles(db, owner_id=owner_id)
    remaining_by_key = {b.key: b for b in remaining}

    for key in bundle_keys:
        if key in remaining_by_key:
            parts.append(render_bundle_group_oob(request, remaining_by_key[key], db))
        else:
            parts.append(
                f'<div id="triage-row-{key}" hx-swap-oob="delete"></div>'
                f'<div id="triage-row-expanded-{key}" hx-swap-oob="delete"></div>'
            )

    parts.append(render_sidebar_badges_oob(db, owner_id=owner_id))
    parts.append(
        render_triage_header_stats_oob(
            request, db, bundles=remaining, owner_id=owner_id
        )
    )
    return "".join(parts)

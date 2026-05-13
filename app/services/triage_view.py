"""View-layer aggregator for the redesigned triage page.

Consumes `BundleView` and produces UI-shaped objects: sub-bundles for the
inline expand and drawer spine, mock_status for the filter chips, header stats,
and date label formatting. Keeps `TriageService` and `BundleView` storage logic
untouched.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from app.constants import SIG_ORDER as _SIG_ORDER  # noqa: E402
from app.models.database import Document
from app.models.enums import (
    DocumentRole,
    PipelineState,
)

STATUS_STUCK = "stuck"
STATUS_PROCESSING = "processing"
STATUS_NEEDS_CLASSIFICATION = "needs_classification"
STATUS_NEEDS_REVIEW = "needs_review"


@dataclass
class SubBundleView:
    """One parent-root subtree inside a `BundleView`, shaped for the redesigned UI."""

    id: str  # f"{bundle.key}-g{group_index}"
    label: str  # cover doc title or "Group A"
    lead_doc: Document | None  # None for empty groups
    docs: list[tuple[int, Document]] = field(default_factory=list)  # (depth, doc)
    suggested_case_id: str | None = None
    suggested_case_title: str | None = None
    field_confidence_case: str | None = None  # "high" | "medium" | "low" | None
    sub_group_id: int | None = None  # DB id of BatchSubGroup; None in auto mode

    @property
    def doc_count(self) -> int:
        return len(self.docs)


def _pick_lead_doc(group: list[tuple[int, Document]]) -> Document | None:
    """Cover-letter wins; else most-significant; else lowest id (stable). None for empty group."""
    docs = [d for _, d in group]
    if not docs:
        return None
    cover = next((d for d in docs if d.role == DocumentRole.COVER_LETTER), None)
    if cover:
        return cover
    return min(
        docs,
        key=lambda d: (
            _SIG_ORDER.get(d.significance_tier, 99),
            d.id or 0,
        ),
    )


def _label_for_group(lead: Document | None, fallback_index: int) -> str:
    if lead and lead.title:
        return lead.title
    return f"Group {chr(ord('A') + fallback_index)}"


def _majority_case(docs: list[Document]) -> str | None:
    candidates = [d.case_id for d in docs if d.case_id and d.case_id != "_TRIAGE"]
    if not candidates:
        return None
    counts = Counter(candidates)
    return counts.most_common(1)[0][0]


def build_sub_bundles(bundle) -> list[SubBundleView]:  # bundle: BundleView
    """Aggregate bundle documents into SubBundleView rows.

    Auto mode: uses bundle.parent_groups (existing parent_id hierarchy).
    Manual mode: activates when any doc has sub_group_id set; uses BatchSubGroup rows.
    """
    docs_with_sg = [d for d in bundle.documents if d.sub_group_id is not None]
    if docs_with_sg and bundle.batch_id:
        return _build_sub_bundles_manual(bundle)
    return _build_sub_bundles_auto(bundle)


def _build_sub_bundles_auto(bundle) -> list[SubBundleView]:
    """Original auto logic using parent_groups."""
    sub_bundles: list[SubBundleView] = []
    groups = bundle.parent_groups or []
    if not groups and bundle.documents:
        groups = [[(0, d) for d in bundle.documents]]

    for idx, group in enumerate(groups):
        if not group:
            continue
        lead = _pick_lead_doc(group)
        leaf_docs = [d for _, d in group]
        suggested_case = _majority_case(leaf_docs) or bundle.suggested_case_id
        suggested_title = (
            bundle.suggested_case_title
            if suggested_case == bundle.suggested_case_id
            else None
        )
        # When the case has already been ratified, `bundle.suggested_case_id`
        # is intentionally left None (suppresses the "Confirm case" footer),
        # but `bundle.suggested_case_title` still carries the real title.
        # Without this fallback the modal would render an empty `—` for
        # already-confirmed cases.
        if (
            not suggested_title
            and suggested_case
            and suggested_case == bundle.confirmed_case_id
        ):
            suggested_title = bundle.suggested_case_title
        confidence = (lead.extraction_confidence or {}).get("case_id") if lead else None
        sub_bundles.append(
            SubBundleView(
                id=f"{bundle.key}-g{idx}",
                label=_label_for_group(lead, idx)
                if lead
                else f"Group {chr(ord('A') + idx)}",
                lead_doc=lead,
                docs=group,
                suggested_case_id=suggested_case,
                suggested_case_title=suggested_title,
                field_confidence_case=confidence,
                sub_group_id=None,
            )
        )
    return sub_bundles


def _build_sub_bundles_manual(bundle) -> list[SubBundleView]:
    """Manual mode: group by sub_group_id, ordered by BatchSubGroup.sort_order."""
    from collections import defaultdict

    groups_by_sgid: dict[int, list] = defaultdict(list)
    sg_meta: dict[int, object] = {}

    for d in sorted(
        bundle.documents, key=lambda x: (x.sub_group_sort_order or 0, x.id)
    ):
        if d.sub_group_id is not None:
            groups_by_sgid[d.sub_group_id].append((0, d))
            if d.sub_group and d.sub_group_id not in sg_meta:
                sg_meta[d.sub_group_id] = d.sub_group

    def sg_sort_key(sgid: int) -> int:
        sg = sg_meta.get(sgid)
        return sg.sort_order if sg else 0

    ordered_sgids = sorted(groups_by_sgid.keys(), key=sg_sort_key)

    sub_bundles: list[SubBundleView] = []
    for idx, sgid in enumerate(ordered_sgids):
        group = groups_by_sgid[sgid]
        sg = sg_meta.get(sgid)
        lead = _pick_lead_doc(group)
        leaf_docs = [d for _, d in group]
        suggested_case = _majority_case(leaf_docs) or bundle.suggested_case_id
        suggested_title = (
            bundle.suggested_case_title
            if suggested_case == bundle.suggested_case_id
            else None
        )
        if (
            not suggested_title
            and suggested_case
            and suggested_case == bundle.confirmed_case_id
        ):
            suggested_title = bundle.suggested_case_title
        confidence = (lead.extraction_confidence or {}).get("case_id") if lead else None

        label: str
        if sg and sg.label:
            label = sg.label
        elif lead:
            label = _label_for_group(lead, idx)
        else:
            label = f"Group {chr(ord('A') + idx)}"

        sub_bundles.append(
            SubBundleView(
                id=f"{bundle.key}-g{sgid}",
                label=label,
                lead_doc=lead,
                docs=group,
                suggested_case_id=suggested_case,
                suggested_case_title=suggested_title,
                field_confidence_case=confidence,
                sub_group_id=sgid,
            )
        )

    # Orphaned docs (sub_group_id=None while manual mode is active) → prepend to first group.
    ungrouped = [(0, d) for d in bundle.documents if d.sub_group_id is None]
    if ungrouped:
        if sub_bundles:
            first = sub_bundles[0]
            sub_bundles[0] = SubBundleView(
                id=first.id,
                label=first.label,
                lead_doc=first.lead_doc,
                docs=ungrouped + first.docs,
                suggested_case_id=first.suggested_case_id,
                suggested_case_title=first.suggested_case_title,
                field_confidence_case=first.field_confidence_case,
                sub_group_id=first.sub_group_id,
            )
        else:
            lead = _pick_lead_doc(ungrouped)
            sub_bundles.append(
                SubBundleView(
                    id=f"{bundle.key}-g0",
                    label=_label_for_group(lead, 0) if lead else "Group A",
                    lead_doc=lead,
                    docs=ungrouped,
                    suggested_case_id=bundle.suggested_case_id,
                    suggested_case_title=bundle.suggested_case_title,
                    field_confidence_case=None,
                    sub_group_id=None,
                )
            )

    return sub_bundles


def mock_status(bundle) -> str:  # bundle: BundleView
    """Return one of: 'stuck', 'processing', 'needs_classification', 'needs_review'.

    First match wins:
      stuck:                any d.pipeline_state == FAILED
      processing:           any d.pipeline_state in (PENDING, RUNNING, PARTIAL)
      needs_classification: not confirmed and no suggested case
      needs_review:         otherwise

    PARTIAL covers the between-stage gap: some stages have completed but later
    stages are still pending (e.g. after batch_analysis sets suggested_case_id
    while enrich/relationships/claims/entities/embeddings are queued). Without
    PARTIAL the row would flip to needs_review and unlock Confirm mid-pipeline.
    """
    states = {d.pipeline_state for d in bundle.documents if d.pipeline_state}
    if PipelineState.FAILED in states:
        return STATUS_STUCK
    if states & {PipelineState.PENDING, PipelineState.RUNNING, PipelineState.PARTIAL}:
        return STATUS_PROCESSING
    if not bundle.confirmed_case_id and not bundle.suggested_case_id:
        return STATUS_NEEDS_CLASSIFICATION
    return STATUS_NEEDS_REVIEW


def stats_for_chips(bundles: list) -> dict:
    """Header chip counts.

    `pending` = bundles still in the queue (any of the four statuses);
    `completed_today` = placeholder 0 until a "completed today" query lands
    (the triage feed only carries the unfinished queue today, so we cannot
    derive the count from `bundles` alone).
    """
    counts = {
        STATUS_NEEDS_CLASSIFICATION: 0,
        STATUS_NEEDS_REVIEW: 0,
        STATUS_STUCK: 0,
        STATUS_PROCESSING: 0,
    }
    for bundle in bundles:
        counts[mock_status(bundle)] += 1
    return {
        "pending": sum(counts.values()),
        "completed_today": 0,
        **counts,
    }


# -----------------------------------------------------------------------------
# OOB render helpers
#
# Moved out of `app/api/triage.py` so other API modules (cases, documents) can
# import them without coupling to a sibling route module. Imports of
# TriageService/BundleView are kept lazy to avoid a circular import — the
# service module already lazy-imports back into this module.
# -----------------------------------------------------------------------------

from fastapi import Request  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import templates  # noqa: E402
from app.constants import ORIGINATOR_COLORS, ORIGINATOR_ICONS  # noqa: E402
from app.models.enums import (  # noqa: E402
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


def failed_doc_summary(bundles) -> tuple[int, int | None]:
    """Return (count, first_failed_doc_id) for docs with pipeline_state=failed
    across the bundles list. Used by the status bar chip + the page-render
    context so the same source-of-truth flows to both."""
    failed_count = 0
    first_failed_doc_id: int | None = None
    for b in bundles:
        for d in b.documents:
            if d.pipeline_state == PipelineState.FAILED:
                failed_count += 1
                if first_failed_doc_id is None:
                    first_failed_doc_id = d.id
    return failed_count, first_failed_doc_id

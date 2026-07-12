"""View-layer aggregator for the redesigned triage page.

Consumes `BundleView` and produces UI-shaped objects: sub-bundles for the
inline expand and drawer spine, mock_status for the filter chips, header stats,
and date label formatting. Keeps `BundleView` storage logic
untouched.
"""

from collections import Counter
from dataclasses import dataclass, field

from app.constants import SIG_ORDER as _SIG_ORDER
from app.models.database import BatchSubGroup, Document
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
    """Pick the substantive lead doc for a sub-group. None for empty group.

    Cover letters are wrappers ('cover letters are relays; collapse the wrapper').
    The enclosed substantive doc owns the group's identity for both the bolded
    label and the default HUD focus.

    Ranking key (lowest tuple wins):
      1. significance_tier  — critical < significant < informational < administrative
      2. is_cover_letter    — substantive beats wrapper within the same tier
      3. id                 — stable tiebreak
    """
    docs = [d for _, d in group]
    if not docs:
        return None
    return min(
        docs,
        key=lambda d: (
            _SIG_ORDER.get(d.significance_tier, 99)
            if d.significance_tier is not None
            else 99,
            1 if d.role == DocumentRole.COVER_LETTER else 0,
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
    sg_meta: dict[int, BatchSubGroup] = {}

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

    # Include empty sub-groups (no docs yet, e.g. freshly created by "New Group")
    if getattr(bundle, "sub_groups", None):
        known_ids = set(ordered_sgids)
        for sg in sorted(bundle.sub_groups, key=lambda s: s.sort_order):
            if sg.id not in known_ids:
                ordered_sgids.append(sg.id)
                sg_meta[sg.id] = sg

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

"""CaseGraphService — builds the JSON-serializable graph payload for the swim-lane SVG renderer.

All geometry (x/y coordinates, path strings for edges) is computed here so the
Jinja template is pure rendering with no logic.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session, joinedload

from app.models.database import Document
from app.models.enums import (
    DocumentRole,
    OriginatorType,
    RelationshipType,
    SignificanceTier,
)
from app.repositories.document import DocumentRepository
from app.repositories.document_relationship import DocumentRelationshipRepository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Geometry constants — must match the SVG template exactly
# ---------------------------------------------------------------------------
LANE_W = 225
ROW_H = 80
TOP = 32
LEFT = 36
NODE_W = 180
NODE_H = 50
GHOST_NODE_H = 56

# ---------------------------------------------------------------------------
# Lane definitions (fixed order)
# ---------------------------------------------------------------------------
LANES = [
    {"key": "own", "label": "YOU", "color": "own"},
    {"key": "court", "label": "COURT", "color": "court"},
    {"key": "opposing", "label": "OPPOSING", "color": "opposing"},
    {"key": "third", "label": "THIRD PARTY", "color": "third"},
]

_LANE_INDEX = {lane["key"]: i for i, lane in enumerate(LANES)}

# OriginatorType → lane key
_ORIGINATOR_LANE: dict[str, str] = {
    OriginatorType.COURT: "court",
    OriginatorType.OPPOSING: "opposing",
    OriginatorType.OWN: "own",
    OriginatorType.THIRD_PARTY: "third",
    OriginatorType.UNKNOWN: "own",  # fallback
}


# ---------------------------------------------------------------------------
# GraphPayload dataclass
# ---------------------------------------------------------------------------
@dataclass
class GraphPayload:
    lanes: list[dict]
    nodes: list[dict]
    bundles: list[dict]
    edges: list[dict]
    proof_badges: dict
    svg_width: int
    svg_height: int
    node_counts: dict
    filter: str
    node_count: int
    edge_count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clip(s: str, n: int) -> str:
    """Clip string to n characters, appending ellipsis if truncated."""
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _lane_for(doc) -> str:
    """Return the lane key for a document based on its originator_type.

    When attributed_originator holds a role key (e.g. "opposing"), it
    overrides originator_type to route court-relay docs to the correct lane.
    Human display names stored in attributed_originator silently fall back
    to originator_type.
    """
    originator = doc.originator_type
    if doc.attributed_originator:
        try:
            originator = OriginatorType(doc.attributed_originator)
        except ValueError:
            pass  # display name, not a role key — fall back to originator_type
    if originator is None:
        logger.warning(
            "Document %s has no originator_type, defaulting to 'own'", doc.id
        )
        return "own"
    lane = _ORIGINATOR_LANE.get(originator)
    if lane is None:
        logger.warning(
            "Document %s has unexpected originator_type %r, defaulting to 'own'",
            doc.id,
            originator,
        )
        return "own"
    if originator == OriginatorType.UNKNOWN:
        logger.warning(
            "Document %s has UNKNOWN originator_type, defaulting to 'own'", doc.id
        )
    return lane


def _is_potential_bundle_header(doc) -> bool:
    """A relay doc that *could* head a bundle. Whether it actually renders as
    a bundle depends on having at least one child (resolved later).
    """
    if doc.role == DocumentRole.COVER_LETTER:
        return True
    return bool(doc.court_relay)


def passes_filter(doc, filter_mode: str) -> bool:
    """Return True if the document passes the significance filter."""
    if filter_mode == "critical":
        return doc.significance_tier == SignificanceTier.CRITICAL
    if filter_mode == "significant+":
        if doc.significance_tier == SignificanceTier.ADMINISTRATIVE:
            # Relay bundles are always visible even in significant+ mode
            return doc.role == DocumentRole.COVER_LETTER
        return True
    # "all" keeps everything
    return True


def compute_edge_path(from_node: dict, to_node: dict) -> str:
    """Compute SVG path string for an edge between two nodes (Bezier routing)."""
    # Start/End is center-middle of cards
    ax = from_node["x"] + NODE_W / 2
    ay = from_node["y"] + from_node["h"] / 2
    bx = to_node["x"] + NODE_W / 2
    by = to_node["y"] + to_node["h"] / 2

    dx = bx - ax
    dy = by - ay
    same_lane = abs(dx) < 2

    # If same lane and far apart, cards have a vertical entry/exit
    if same_lane:
        # Exit bottom of A, enter top of B
        path = f"M {ax} {ay + from_node['h'] / 2} L {bx} {by - to_node['h'] / 2}"
    else:
        # Cubic bezier for cross-lane movement.
        # Exit/Enter from side edges
        ax_edge = ax + (NODE_W / 2 if dx > 0 else -NODE_W / 2)
        bx_edge = bx + (-NODE_W / 2 if dx > 0 else NODE_W / 2)

        # Control points at 50% horizontal distance
        cp1x = ax_edge + (dx * 0.4)
        cp2x = bx_edge - (dx * 0.4)

        # But if vertical distance is large, we force the tangents to be more vertical
        # to avoid the "broken arrow" problem where the line enters at a sharp angle.
        if abs(dy) > ROW_H:
            # S-curve with more verticality
            path = f"M {ax_edge} {ay} C {cp1x} {ay}, {cp2x} {by}, {bx_edge} {by}"
        else:
            path = f"M {ax_edge} {ay} C {ax_edge + dx / 2} {ay}, {bx_edge - dx / 2} {by}, {bx_edge} {by}"

    return path


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------


class CaseGraphService:
    def __init__(self, db: Session):
        self.db = db
        self.doc_repo = DocumentRepository(db)
        self.rel_repo = DocumentRelationshipRepository(db)

    def build_payload(
        self,
        proceeding_id: int,
        significance_filter: Literal[
            "critical", "significant+", "all"
        ] = "significant+",
        new_doc_ids: set[int] | None = None,
        reaction_map: dict[int, str] | None = None,
    ) -> GraphPayload:
        """Build the complete graph payload for the given proceeding."""
        if new_doc_ids is None:
            new_doc_ids = set()
        if reaction_map is None:
            reaction_map = {}

        # Fetch all docs for the proceeding (no tier filter — filter in Python
        # so we can accurately count hidden docs).
        # Eager load proceeding to avoid N+1 in templates/logic
        all_docs = self.doc_repo.get_by_proceeding(
            proceeding_id, options=[joinedload(Document.proceeding)]
        )

        # ------------------------------------------------------------------
        # Identify bundle headers and their children.
        # Children come from explicit parent_id wiring first; for batches with
        # exactly one court-relay doc and unwired siblings (the common
        # "court letter + attachments in one email" shape that the batch
        # analyzer doesn't always wire), siblings are inferred as children.
        # ------------------------------------------------------------------
        candidate_header_ids: set[int] = {
            doc.id for doc in all_docs if _is_potential_bundle_header(doc)
        }
        bundle_children: dict[int, list] = {}

        for doc in all_docs:
            if doc.parent_id is not None and doc.parent_id in candidate_header_ids:
                bundle_children.setdefault(doc.parent_id, []).append(doc)

        # Inference fallback: a single court_relay doc in a batch claims its
        # parentless siblings.
        docs_by_batch: dict[int, list] = {}
        for doc in all_docs:
            if doc.ingest_batch_id is not None:
                docs_by_batch.setdefault(doc.ingest_batch_id, []).append(doc)

        for batch_docs in docs_by_batch.values():
            relays = [d for d in batch_docs if d.court_relay]
            if len(relays) != 1 or len(batch_docs) <= 1:
                continue
            relay = relays[0]
            if relay.id in bundle_children:
                continue  # Already wired explicitly
            inferred = [
                d for d in batch_docs if d.id != relay.id and d.parent_id is None
            ]
            if inferred:
                bundle_children[relay.id] = inferred

        bundle_header_ids: set[int] = set(bundle_children.keys())
        child_doc_ids: set[int] = {
            child.id for children in bundle_children.values() for child in children
        }

        # ------------------------------------------------------------------
        # Apply significance filter; exclude bundle children (they live inside
        # their bundle node, not as standalone rows).
        # ------------------------------------------------------------------
        visible_docs = [
            doc
            for doc in all_docs
            if doc.id not in child_doc_ids and passes_filter(doc, significance_filter)
        ]

        # Per-tier counts for Alpine hiddenCount() — mirrors isNodeHidden() logic.
        node_counts: dict[str, int] = {
            "critical": 0,
            "significant": 0,
            "informational": 0,
            "administrative_standalone": 0,
            "administrative_relay": 0,
        }
        for d in all_docs:
            if d.id in child_doc_ids:
                continue
            tier = d.significance_tier.value if d.significance_tier else "informational"
            if tier == "administrative":
                if d.role == DocumentRole.COVER_LETTER:
                    node_counts["administrative_relay"] += 1
                else:
                    node_counts["administrative_standalone"] += 1
            elif tier in node_counts:
                node_counts[tier] += 1
            else:
                node_counts["informational"] += 1

        # ------------------------------------------------------------------
        # Pre-fetch cross-proceeding ghost docs so they can be merged inline
        # into the sorted timeline rather than stacked above it.
        # ------------------------------------------------------------------
        relationships = self.rel_repo.get_for_proceeding(proceeding_id)
        visible_doc_ids = {doc.id for doc in visible_docs}
        all_proceeding_doc_ids = {doc.id for doc in all_docs}
        external_doc_ids: set[int] = set()
        for rel in relationships:
            if (
                rel.from_document_id not in visible_doc_ids
                and rel.from_document_id not in child_doc_ids
                and rel.from_document_id not in all_proceeding_doc_ids
            ):
                external_doc_ids.add(rel.from_document_id)
            if (
                rel.to_document_id not in visible_doc_ids
                and rel.to_document_id not in child_doc_ids
                and rel.to_document_id not in all_proceeding_doc_ids
            ):
                external_doc_ids.add(rel.to_document_id)

        external_doc_map: dict[int, Document] = {}
        if external_doc_ids:
            ext_list = (
                self.db.query(Document)
                .options(joinedload(Document.proceeding))
                .filter(Document.id.in_(external_doc_ids))
                .all()
            )
            external_doc_map = {d.id: d for d in ext_list}

        # Merge external docs into the timeline, maintaining SQL sort order:
        # issued_date ASC NULLS LAST, id ASC.
        all_timeline_docs = sorted(
            list(visible_docs) + list(external_doc_map.values()),
            key=lambda d: (d.issued_date is None, d.issued_date or datetime.min, d.id),
        )

        # ------------------------------------------------------------------
        # Row assignment: docs are sorted by issued_date ASC NULLS LAST,
        # then id ASC. External ghost docs are merged inline by date.
        # ------------------------------------------------------------------
        nodes: list[dict] = []
        node_by_id: dict[int, dict] = {}

        for row_index, doc in enumerate(all_timeline_docs):
            is_cross_proceeding = doc.id in external_doc_map
            lane_key = _lane_for(doc)
            lane_idx = _LANE_INDEX[lane_key]
            x = LEFT + lane_idx * LANE_W + (LANE_W - NODE_W) / 2
            y = TOP + row_index * ROW_H

            if is_cross_proceeding:
                node: dict = {
                    "id": doc.id,
                    "lane": lane_key,
                    "row": row_index,
                    "x": x,
                    "y": y,
                    "w": NODE_W,
                    "h": GHOST_NODE_H,
                    "title": _clip(doc.title or "Untitled", 18),
                    "full_title": doc.title or "Untitled",
                    "role": doc.role.value if doc.role else "standalone",
                    "date_short": doc.issued_date.strftime("%m-%d")
                    if doc.issued_date
                    else "\u2014",
                    "tier": "administrative",
                    "thread_open": False,
                    "ghost": True,
                    "cross_proceeding": True,
                    "proceeding_label": doc.proceeding.az_court
                    if doc.proceeding
                    else "External",
                    "is_bundle": False,
                    "is_new_since_last_visit": False,
                    "reaction": None,
                    "court_relay": False,
                    "originator_type": doc.originator_type.value
                    if doc.originator_type
                    else "unknown",
                }
            else:
                # Determine max title length based on available flags to prevent overlap
                # Card width is 180px. Start X is 15px.
                # Critical flag is at 163px. Reaction is at 174px.
                max_title_len = 21
                has_reaction = bool(reaction_map.get(doc.id))
                is_critical = doc.significance_tier == SignificanceTier.CRITICAL

                if has_reaction:
                    max_title_len = 16
                if is_critical:
                    # Tighten to 14 (from 15) to be safer with variable-width fonts
                    max_title_len = min(max_title_len, 14)

                node = {
                    "id": doc.id,
                    "lane": lane_key,
                    "row": row_index,
                    "x": x,
                    "y": y,
                    "w": NODE_W,
                    "h": NODE_H,
                    "title": _clip(doc.title or "Untitled", max_title_len),
                    "full_title": doc.title or "Untitled",
                    "role": doc.role.value if doc.role else "standalone",
                    "date_short": doc.issued_date.strftime("%m-%d")
                    if doc.issued_date
                    else "\u2014",
                    "tier": doc.significance_tier.value
                    if doc.significance_tier
                    else "informational",
                    "thread_open": bool(doc.thread_open)
                    if hasattr(doc, "thread_open")
                    else False,
                    "ghost": doc.issued_date is None,
                    "cross_proceeding": False,
                    "proceeding_label": None,
                    "is_bundle": doc.id in bundle_header_ids,
                    "is_new_since_last_visit": doc.id in new_doc_ids,
                    "reaction": reaction_map.get(doc.id),
                    "court_relay": bool(doc.court_relay),
                    "originator_type": doc.originator_type.value
                    if doc.originator_type
                    else "unknown",
                }
            nodes.append(node)
            node_by_id[doc.id] = node

        # ------------------------------------------------------------------
        # Bundle dicts (for each bundle header that is in the visible set)
        # ------------------------------------------------------------------
        bundles: list[dict] = []

        for node in nodes:
            if not node["is_bundle"]:
                continue
            bundle_doc_id = node["id"]
            # Find the original doc object
            doc_obj = next(d for d in all_docs if d.id == bundle_doc_id)
            children = bundle_children.get(bundle_doc_id, [])

            bundle_lane_idx = _LANE_INDEX[_lane_for(doc_obj)]
            bundle = {
                "id": bundle_doc_id,
                "lane": _lane_for(doc_obj),
                "row": node["row"],
                "x": LEFT + bundle_lane_idx * LANE_W + (LANE_W - NODE_W) / 2 - 6,
                "y": TOP + node["row"] * ROW_H - 6,
                "header": "\u2691 COURT RELAY",
                "footer": (
                    f"zugestellt {doc_obj.issued_date.strftime('%d.%m')}"
                    if doc_obj.issued_date
                    else "zugestellt \u2014"
                ),
                "children": [
                    {
                        "id": child.id,
                        "title": _clip(child.title or "Untitled", 22),
                        "origin": _lane_for(child),
                    }
                    for child in children
                ],
            }
            bundles.append(bundle)

        # ------------------------------------------------------------------
        # Edges for cross-proceeding and same-proceeding references
        # ------------------------------------------------------------------
        proof_badges: dict[int, int] = {}
        edges: list[dict] = []

        _REL_STYLE: dict[str, dict] = {
            RelationshipType.REPLIES_TO: {
                "kind": "reply",
                "dashed": False,
                "stroke_w": 1.0,
                "arrow": True,
            },
            RelationshipType.REFERENCES: {
                "kind": "reference",
                "dashed": True,
                "stroke_w": 1.0,
                "arrow": True,
            },
            RelationshipType.SUPERSEDES: {
                "kind": "supersede",
                "dashed": False,
                "stroke_w": 0.5,
                "arrow": True,
            },
        }

        for rel in relationships:
            from_id = rel.from_document_id
            to_id = rel.to_document_id

            rel_type = rel.relationship_type

            # ATTACHES_AS_PROOF → proof badge, not an edge
            if rel_type == RelationshipType.ATTACHES_AS_PROOF:
                proof_badges[to_id] = proof_badges.get(to_id, 0) + 1
                continue

            # CITED_BY → skip (inverse relationship)
            if rel_type == RelationshipType.CITED_BY:
                continue

            # Skip edges involving bundle children (they are not standalone nodes)
            if from_id in child_doc_ids or to_id in child_doc_ids:
                continue

            # Only include edges where both endpoints are in the filtered nodes
            from_node = node_by_id.get(from_id)
            to_node = node_by_id.get(to_id)
            if from_node is None or to_node is None:
                continue

            style = _REL_STYLE.get(rel_type)
            if style is None:
                # Unknown relationship type — skip
                logger.warning("Unknown relationship type %r, skipping edge", rel_type)
                continue

            path = compute_edge_path(from_node, to_node)

            edge = {
                "id": f"{from_id}-{to_id}",
                "from_node_id": from_id,
                "to_node_id": to_id,
                "kind": style["kind"],
                "path": path,
                "dashed": style["dashed"],
                "dasharray": "4 3" if style["dashed"] else "",
                "stroke_w": style["stroke_w"],
                "arrow": style["arrow"],
            }
            edges.append(edge)

        # ------------------------------------------------------------------
        # SVG dimensions
        # ------------------------------------------------------------------
        total_rows = (
            (max((n.get("row", 0) for n in nodes), default=0) + 1) if nodes else 1
        )
        svg_height = TOP + total_rows * ROW_H + 120
        child_lanes = {
            _lane_for(child)
            for children in bundle_children.values()
            for child in children
        }
        active_lane_keys = (
            {n["lane"] for n in nodes} | {b["lane"] for b in bundles} | child_lanes
        )
        max_lane_idx = (
            max(_LANE_INDEX[k] for k in active_lane_keys)
            if active_lane_keys
            else len(LANES) - 1
        )
        svg_width = LEFT * 2 + (max_lane_idx + 1) * LANE_W

        visible_lanes = [
            lane for lane in LANES if _LANE_INDEX[lane["key"]] <= max_lane_idx
        ]
        return GraphPayload(
            lanes=visible_lanes,
            nodes=nodes,
            bundles=bundles,
            edges=edges,
            proof_badges=proof_badges,
            svg_width=svg_width,
            svg_height=svg_height,
            node_counts=node_counts,
            filter=significance_filter,
            node_count=len(nodes),
            edge_count=len(edges),
        )

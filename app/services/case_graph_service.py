"""CaseGraphService — builds the JSON-serializable graph payload for the swim-lane SVG renderer.

All geometry (x/y coordinates, path strings for edges) is computed here so the
Jinja template is pure rendering with no logic.
"""

import logging
from dataclasses import dataclass

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
LANE_W = 180
ROW_H = 64
TOP = 64
LEFT = 36
NODE_W = 144
NODE_H = 40

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
    hidden_counts: dict
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
    """Return the lane key for a document based on its originator_type."""
    if doc.originator_type is None:
        logger.warning(
            "Document %s has no originator_type, defaulting to 'own'", doc.id
        )
        return "own"
    lane = _ORIGINATOR_LANE.get(doc.originator_type)
    if lane is None:
        logger.warning(
            "Document %s has unexpected originator_type %r, defaulting to 'own'",
            doc.id,
            doc.originator_type,
        )
        return "own"
    if doc.originator_type == OriginatorType.UNKNOWN:
        logger.warning(
            "Document %s has UNKNOWN originator_type, defaulting to 'own'", doc.id
        )
    return lane


def _is_bundle_header(doc) -> bool:
    """True if the document is a court-relay cover-letter bundle header."""
    return bool(doc.court_relay) and doc.role == DocumentRole.COVER_LETTER


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
    ax = from_node["x"] + NODE_W / 2
    ay = from_node["y"] + NODE_H / 2
    bx = to_node["x"] + NODE_W / 2
    by = to_node["y"] + NODE_H / 2
    dx = bx - ax
    same_lane = abs(dx) < 2
    if same_lane:
        path = f"M {ax} {ay + NODE_H / 2} L {bx} {by - NODE_H / 2}"
    else:
        mx = ax + dx / 2
        ax_edge = ax + (NODE_W / 2 if dx > 0 else -NODE_W / 2)
        bx_edge = bx + (-NODE_W / 2 if dx > 0 else NODE_W / 2)
        path = f"M {ax_edge} {ay} C {mx} {ay}, {mx} {by}, {bx_edge} {by}"
    return path


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------


class CaseGraphService:
    def __init__(self, db):
        self.db = db
        self.doc_repo = DocumentRepository(db)
        self.rel_repo = DocumentRelationshipRepository(db)

    def build_payload(
        self,
        proceeding_id: int,
        significance_filter: str = "significant+",
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
        all_docs = self.doc_repo.get_by_proceeding(proceeding_id)

        # ------------------------------------------------------------------
        # Identify bundle headers and their children
        # ------------------------------------------------------------------
        bundle_header_ids: set[int] = set()
        # Map: bundle_header_id → list of child docs
        bundle_children: dict[int, list] = {}

        for doc in all_docs:
            if _is_bundle_header(doc):
                bundle_header_ids.add(doc.id)
                bundle_children.setdefault(doc.id, [])

        # Collect children (docs whose parent_id points to a bundle header)
        child_doc_ids: set[int] = set()
        for doc in all_docs:
            if doc.parent_id is not None and doc.parent_id in bundle_header_ids:
                bundle_children.setdefault(doc.parent_id, []).append(doc)
                child_doc_ids.add(doc.id)

        # ------------------------------------------------------------------
        # Compute hidden counts (against "significant+" filter, before user filter)
        # ------------------------------------------------------------------
        hidden_counts: dict[str, int] = {"administrative": 0, "informational": 0}
        for doc in all_docs:
            if doc.significance_tier == SignificanceTier.ADMINISTRATIVE:
                if doc.role != DocumentRole.COVER_LETTER:
                    hidden_counts["administrative"] += 1
            elif doc.significance_tier == SignificanceTier.INFORMATIONAL:
                hidden_counts["informational"] += 1

        # ------------------------------------------------------------------
        # Apply significance filter; exclude bundle children (they live inside
        # their bundle node, not as standalone rows).
        # ------------------------------------------------------------------
        visible_docs = [
            doc
            for doc in all_docs
            if doc.id not in child_doc_ids and passes_filter(doc, significance_filter)
        ]

        # ------------------------------------------------------------------
        # Row assignment: docs are already sorted by received_date ASC NULLS LAST,
        # then id ASC (guaranteed by get_by_proceeding).
        # ------------------------------------------------------------------
        nodes: list[dict] = []
        node_by_id: dict[int, dict] = {}

        for row_index, doc in enumerate(visible_docs):
            lane_key = _lane_for(doc)
            lane_idx = _LANE_INDEX[lane_key]
            x = LEFT + lane_idx * LANE_W + (LANE_W - NODE_W) / 2
            y = TOP + row_index * ROW_H

            node: dict = {
                "id": doc.id,
                "lane": lane_key,
                "row": row_index,
                "x": x,
                "y": y,
                "w": NODE_W,
                "h": NODE_H,
                "title": _clip(doc.title or "Untitled", 17),
                "full_title": doc.title or "Untitled",
                "role": doc.role.value if doc.role else "standalone",
                "date_short": doc.received_date.strftime("%m-%d")
                if doc.received_date
                else "\u2014",
                "tier": doc.significance_tier.value
                if doc.significance_tier
                else "informational",
                "thread_open": bool(doc.thread_open)
                if hasattr(doc, "thread_open")
                else False,
                "ghost": doc.received_date is None,
                "is_bundle": _is_bundle_header(doc),
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
        court_lane_idx = _LANE_INDEX["court"]

        for node in nodes:
            if not node["is_bundle"]:
                continue
            bundle_doc_id = node["id"]
            # Find the original doc object
            doc_obj = next(d for d in all_docs if d.id == bundle_doc_id)
            children = bundle_children.get(bundle_doc_id, [])

            bundle = {
                "id": bundle_doc_id,
                "lane": "court",
                "row": node["row"],
                "x": LEFT + court_lane_idx * LANE_W + (LANE_W - NODE_W) / 2 - 6,
                "y": TOP + node["row"] * ROW_H - 6,
                "header": "\u2691 COURT RELAY",
                "footer": (
                    f"zugestellt {doc_obj.received_date.strftime('%d.%m')}"
                    if doc_obj.received_date
                    else "zugestellt \u2014"
                ),
                "children": [
                    {
                        "id": child.id,
                        "title": _clip(child.title or "Untitled", 18),
                        "origin": _lane_for(child),
                    }
                    for child in children
                ],
            }
            bundles.append(bundle)

        # ------------------------------------------------------------------
        # Edges and proof badges
        # ------------------------------------------------------------------
        relationships = self.rel_repo.get_for_proceeding(proceeding_id)

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
                "stroke_w": style["stroke_w"],
                "arrow": style["arrow"],
            }
            edges.append(edge)

        # ------------------------------------------------------------------
        # SVG dimensions
        # ------------------------------------------------------------------
        total_rows = (max((n["row"] for n in nodes), default=0) + 1) if nodes else 1
        svg_height = TOP + total_rows * ROW_H + 120
        svg_width = LEFT * 2 + len(LANES) * LANE_W

        return GraphPayload(
            lanes=LANES,
            nodes=nodes,
            bundles=bundles,
            edges=edges,
            proof_badges=proof_badges,
            svg_width=svg_width,
            svg_height=svg_height,
            hidden_counts=hidden_counts,
            filter=significance_filter,
            node_count=len(nodes),
            edge_count=len(edges),
        )

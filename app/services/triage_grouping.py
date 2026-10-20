"""Shared grouping rules for a bundle's documents.

One source of truth for how a batch's documents are grouped in the bundle
tree, shared by the automatic view (`triage_view`) and the stored
`BatchSubGroup` rows (`triage_subgroups`). The layout a user sees must not
depend on whether the bundle happens to be in automatic or manual grouping
mode, so both paths derive their groups from here.

Rules:
- A *structured* root — a document with children, or a cover letter — heads
  its own group: the cover letter and its enclosures.
- Every *loose* root — no children, not a cover letter — joins one shared
  group of standalone documents, so an unstructured bundle never renders as
  N one-document groups.
"""

from datetime import UTC, datetime

from app.constants import SIG_ORDER as _SIG_ORDER
from app.models.database import Document
from app.models.enums import DocumentRole
from app.services.ingestion import exhibits

STANDALONE_GROUP_LABEL = "Standalone documents"


def document_sort_key(doc: Document) -> tuple:
    """The bundle's significance order: tier, cover letter first, exhibit, ingest date.

    Cover letters win a tier tie so the user's eye lands on the document that
    heads the group first.
    """
    return (
        _SIG_ORDER.get(doc.significance_tier, 99)
        if doc.significance_tier is not None
        else 99,
        0 if doc.role == DocumentRole.COVER_LETTER else 1,
        exhibits.sort_key(doc.meta),
        doc.ingest_date or datetime.min.replace(tzinfo=UTC),
    )


def parent_tree(
    docs: list[Document],
) -> tuple[list[Document], dict[int, list[Document]]]:
    """Split documents into roots and a parent → children map.

    A document is a root when it has no `parent_id` or when its parent is not
    part of `docs` (an orphaned child).
    """
    docs_by_id = {d.id: d for d in docs}
    children_of: dict[int, list[Document]] = {}
    roots: list[Document] = []
    for d in docs:
        if not d.parent_id or d.parent_id not in docs_by_id:
            roots.append(d)
        else:
            children_of.setdefault(d.parent_id, []).append(d)
    return roots, children_of


def depth_map(docs: list[Document]) -> dict[int, int]:
    """Each document's depth in the bundle tree (0 for a root)."""
    roots, children_of = parent_tree(docs)
    depths: dict[int, int] = {}
    queue: list[tuple[int, Document]] = [(0, root) for root in roots]
    while queue:
        depth, node = queue.pop(0)
        depths[node.id] = depth
        queue.extend((depth + 1, c) for c in children_of.get(node.id, []))
    return depths


def is_structured(doc: Document, children_of: dict[int, list[Document]]) -> bool:
    """True when the document heads a group of its own.

    A cover letter heads its group even when it has no enclosures; a document
    with children heads the subtree below it. Everything else stands alone.
    """
    return bool(children_of.get(doc.id)) or doc.role == DocumentRole.COVER_LETTER


def group_bundle_documents(docs: list[Document]) -> list[list[tuple[int, Document]]]:
    """Group a bundle's documents for display.

    `docs` is expected in bundle order (significance first); the groups and
    the documents inside them keep that order. Each group is a list of
    `(depth, doc)` entries, its root first. The loose roots share one group,
    placed where their most significant member falls in the bundle order.
    """
    roots, children_of = parent_tree(docs)
    groups: list[list[tuple[int, Document]]] = []
    loose: list[Document] = []
    loose_at: int | None = None
    for root in roots:
        if not is_structured(root, children_of):
            if loose_at is None:
                loose_at = len(groups)
            loose.append(root)
            continue
        groups.append(_subtree(root, children_of))
    if loose:
        if loose_at is None:
            loose_at = len(groups)
        groups.insert(loose_at, [(0, d) for d in loose])
    if not groups and docs:
        # A `parent_id` cycle leaves no root at all; keep the documents
        # visible in one group rather than dropping them from the bundle.
        groups.append([(0, d) for d in docs])
    return groups


def _subtree(
    root: Document, children_of: dict[int, list[Document]]
) -> list[tuple[int, Document]]:
    """The root and its descendants, breadth-first."""
    docs: list[tuple[int, Document]] = [(0, root)]
    queue: list[tuple[int, Document]] = [(1, c) for c in children_of.get(root.id, [])]
    while queue:
        depth, node = queue.pop(0)
        docs.append((depth, node))
        queue.extend((depth + 1, c) for c in children_of.get(node.id, []))
    return docs


def standalone_group_label(
    docs: list[Document],
    roots: list[Document],
    children_of: dict[int, list[Document]],
) -> str | None:
    """`Standalone documents` for a group that holds only loose documents.

    Loose means: a root of the bundle tree, no children of its own and not a
    cover letter. A document whose parent lives in another group is an
    enclosure of that document, not a standalone one.

    `None` for every other group — the caller falls back to the lead
    document's title.
    """
    if not docs:
        return None
    root_ids = {d.id for d in roots}
    if any(d.id not in root_ids for d in docs):
        return None
    if any(d.role == DocumentRole.COVER_LETTER for d in docs):
        return None
    if any(children_of.get(d.id) for d in docs):
        return None
    return STANDALONE_GROUP_LABEL

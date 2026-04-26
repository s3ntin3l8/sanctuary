# Sanctuary — Correspondence Graph

Companion document to `docs/specs/00_vision.md` §2. Covers the swim-lane SVG graph that is the primary view of every case dashboard — who said what to whom, in what order, at which court level.

---

## Implementation Status

**Last Updated:** April 26, 2026
**Status:** 🟢 IMPLEMENTED (v1 complete)

| Layer | Status |
|---|---|
| `CaseGraphService.build_payload()` — full graph computation (`case_graph_service.py:181-481`) | ✅ |
| `GraphPayload` dataclass — serializes all rendering data to template (`case_graph_service.py:60-72`) | ✅ |
| Lane assignment via `_lane_for(doc)` — originator-type → swim lane | ✅ |
| Attributed-originator override (court-relayed opposing pleadings go to OPPOSING lane) | ✅ |
| Bundle-header detection (`_is_bundle_header`) — court-relay COVER_LETTER collapse | ✅ |
| Significance filter (`passes_filter`) — 3 modes: `critical` / `significant+` / `all` | ✅ |
| Bezier edge routing (`compute_edge_path`) — same-lane straight, cross-lane S-curve | ✅ |
| `partials/dashboard/correspondence_graph.html` — SVG renderer (282 LoC) | ✅ |
| Sticky lane headers + date axis | ✅ |
| Originator stripe on every node card | ✅ |
| Thread-open glow ring (amber dashed border on `thread_open` nodes) | ✅ |
| Significance flag (⚑ on critical nodes) | ✅ |
| Proof badge (attachment count when `ATTACHES_AS_PROOF` edges present) | ✅ |
| Reaction emoji overlay (🚩/✅/🔍/⚖️ from triage) | ✅ |
| Bundle node: collapsible court-relay container with child rows | ✅ |
| Hidden-tier strip (sticky footer, shows count of filtered-out nodes) | ✅ |
| `CaseGraphRenderer` class in `dashboard.js` — pan, zoom, fit, centerCritical | ✅ |
| Node click → Document HUD slide-in | ✅ |
| Node hover → highlight node + incident edges | ✅ |
| Right-click context menu | ✅ |
| Per-proceeding scope (graph is scoped to `active_proceeding`) | ✅ |
| Keyboard: `g` → graph, `f` → fit, `c` → center critical | ✅ |
| Cross-proceeding ghost nodes | ✅ |
| Context menu "Add reaction" + "Copy link" buttons | ⚠ placeholder — UI rendered but handlers not yet wired |
| Edge visual distinction for `SUPERSEDES` type | ⚠ stroke-w 0.5, hard to see in dense graphs |
| `CITED_BY` relationship rendering | N/A — inverse of REFERENCES; intentionally skipped to avoid duplicate arrows |

### Implementation Deviations

| Feature | Vision §2 | Code | Status |
|---|---|---|---|
| Primary view | "Graph is the primary view for every case" | View-mode tab `view='graph'` (default), first tab | ✅ |
| Swim lanes | "Structural layer: who said what to whom" | Four lanes: YOU / COURT / OPPOSING / THIRD PARTY; originator-driven | ✅ |
| Bundle collapse | Court relay as wrapper | COVER_LETTER with `court_relay=True` becomes collapsible bundle container | ✅ |
| Significance filter | "900 letters collapse to ~150 visible nodes" | Three modes; `significant+` default hides administrative non-relay docs | ✅ |
| Attributed originator | "Court is infrastructure — show true sender" | `attributed_originator` overrides `originator_type` for lane placement | ✅ |
| Cross-proceeding edges | "Visually distinct" | Ghost nodes rendered for cross-proceeding references; no special styling yet | ⚠ accepted for v1 — ghost nodes mark the boundary without crossing lanes |
| `CITED_BY` rendering | Not specified | Skipped — inverse of REFERENCES; rendering both would duplicate arrows | ✅ Accepted |
| `ATTACHES_AS_PROOF` edges | Not specified | No edge line; proof badge (attachment count) instead — cleaner than cluttering with arrows | ✅ Accepted |

---

## The core shift

**Traditional document management:** a file list sorted by date. The viewer must mentally reconstruct who wrote what, who it was in response to, and whether a reply was ever sent. The sequence emerges only by reading every document in order.

**Sanctuary Correspondence Graph:** the primary view is a structural map — four swim lanes represent the parties (YOU, COURT, OPPOSING, THIRD PARTY); nodes are documents; arrows show which document replies to or references which. The viewer can see in one glance whether a Klageerwiderung has been answered, whether a court order was relayed, and whether the opposing party's last move has received a response. The graph makes the invisible visible: silence (no reply edge) is as meaningful as speech (a reply edge). The Timeline view exists only as a fallback for the first few minutes of a new case before relationships have been detected.

---

## Layout overview

```
ADV-024-A  Musterklage GmbH vs. XY   [AG Hamburg ▾]   [critical] [significant+] [all]
┌──── view ────────────────────────────────────────────────────────────────────────────┐
│  ⬤ Graph   ◯ Truth Map   ◯ Timeline   ◯ Financials                                 │
└──────────────────────────────────────────────────────────────────────────────────────┘

       YOU              COURT             OPPOSING         THIRD PARTY
  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
  │  Klageschrift│  │              │  │              │  │              │
  │  RA Schmidt  │  │              │  │              │  │              │
  └──────┬───────┘  └──────────────┘  └──────────────┘  └──────────────┘
         │
         │ replies_to
         ▼
  ┌──────────────┐     ⚓ COURT RELAY ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
  │              │     │ Klageerwiderung   │  attaches as proof: [2]    │
  │              │     │ RA Müller         │                             │
  │              │  ←──┘                  └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
  └──────────────┘

  [ 3 administrative documents hidden — show all ]   ← hidden-tier strip
```

---

## 1. `GraphPayload` — the rendering contract

`CaseGraphService.build_payload()` returns a `GraphPayload` dataclass that is the only thing the Jinja template reads. No queries happen in the template.

```python
@dataclass
class GraphPayload:
    lanes:        list[dict]   # Four swim-lane definitions: key, label, color
    nodes:        list[dict]   # Document nodes: position, styling, metadata
    bundles:      list[dict]   # Court-relay bundle headers + collapsed children
    edges:        list[dict]   # Bezier path data per relationship
    proof_badges: dict         # doc_id → ATTACHES_AS_PROOF count
    svg_width:    int          # Viewport width (lanes × 225 px)
    svg_height:   int          # Viewport height (rows × 80 px)
    node_counts:  dict         # Per-tier counts (critical/significant/informational/admin_standalone/admin_relay)
    filter:       str          # Active filter: "critical" | "significant+" | "all"
    node_count:   int          # Total visible nodes
    edge_count:   int          # Total visible edges (used for Timeline auto-fallback)
```

`edge_count` doubles as the Timeline fallback signal: when `edge_count == 0` on first visit and no persisted view preference exists, `case_dashboard_service.py` defaults to `active_view = 'timeline'`.

---

## 2. Lane assignment

Four swim lanes map `OriginatorType` to a column:

| Lane | Key | `OriginatorType` values | Color stripe |
|---|---|---|---|
| YOU | `"own"` | `OWN`, `UNKNOWN` (fallback) | Green |
| COURT | `"court"` | `COURT` | Blue |
| OPPOSING | `"opposing"` | `OPPOSING` | Red |
| THIRD PARTY | `"third"` | `THIRD_PARTY` | Amber |

**Attribution override** (`_lane_for`, `case_graph_service.py:85-118`): if `doc.attributed_originator` is set (e.g. a court-relayed pleading from opposing counsel), it overrides `originator_type` for lane placement. This ensures the document appears in the OPPOSING lane even though it physically arrived via the court's relay.

Lanes without any visible documents are collapsed to zero width; `svg_width` is computed accordingly.

---

## 3. Bundle collapse

A document is a **bundle header** if `doc.court_relay and doc.role == DocumentRole.COVER_LETTER`. Bundle headers collapse all their children (documents with the same `ingest_batch_id` and a `parent_id` pointing to the header) into a single expandable container in the COURT lane.

Bundle rendering:
- **Collapsed**: 40 px header row — "⚓ COURT RELAY" with amber dashed border; click to expand.
- **Expanded**: Header + 32 px per child row + 16 px footer showing "zugestellt DD.MM".
- Child rows carry the true-sender's lane stripe color (not the COURT lane's blue) so the relay's origin is still visible inside the bundle.
- **Proof badge**: if the bundle contains attached exhibits, an attachment-count badge appears on the header.

This preserves the "court is infrastructure" mental model: the court cover letter is not a primary document; it is a routing envelope that groups the substantive pleadings.

---

## 4. Significance filter

Three modes control node visibility:

| Filter | Visible tiers |
|---|---|
| `critical` | `CRITICAL` only |
| `significant+` (default) | `CRITICAL` + `SIGNIFICANT` + `INFORMATIONAL` + relay `ADMINISTRATIVE` (bundle cover letters) |
| `all` | All tiers including standalone `ADMINISTRATIVE` |

Administrative relay documents (court cover letters) are always included in `significant+` so that relay bundles can still render — without the header the children would have no container. `node_counts` tracks `admin_standalone` and `admin_relay` separately to power the hidden-tier strip at the bottom of the SVG.

---

## 5. Edge typing

Each `DocumentRelationship` row maps to a visual encoding:

| `relationship_type` | Arrow style | Notes |
|---|---|---|
| `REPLIES_TO` | Solid, stroke-w 1.0 | Temporal sequential response |
| `REFERENCES` | Dashed (4 3), stroke-w 1.0 | Cites without direct response sequence |
| `SUPERSEDES` | Solid, stroke-w 0.5 | Overwrites a prior document (thin to signal lower prominence) |
| `ATTACHES_AS_PROOF` | **No line** — proof badge | Count incremented on source node; no separate arrow to reduce clutter |
| `CITED_BY` | **Not rendered** | Inverse of REFERENCES; skipping prevents duplicate bidirectional arrows |

Edges from a relay bundle use amber stroke (same as the bundle header color) to visually connect the relay provenance.

---

## 6. Node anatomy

Each node is a 180 × 50 px SVG group rendered in `correspondence_graph.html`:

| Element | Condition | Source |
|---|---|---|
| Card background | Always | `surface-container` fill; `primary` stroke for new (recently ingested) docs |
| Originator stripe | Always | 5 px left bar in originator-type color |
| ⚑ flag | `tier == CRITICAL` | Amber-red at right edge |
| Attachment badge | `proof_badges[id] > 0` | Material icon + count |
| Reaction emoji | `doc.reactions` non-empty | 🚩/✅/🔍/⚖️ at top-right corner |
| Thread-open glow | `node.thread_open == True` | Amber dashed ring |
| Title text | Always | Pre-clipped to 21 chars (server-side); shorter if flag/reaction present |
| Role subtitle | Always | `COVER_LETTER` / `ENCLOSURE` / `STANDALONE` |

**Ghost nodes** are rendered for documents from other proceedings that are referenced by the current proceeding's relationships. They use a transparent fill and dashed stroke to signal "this is context, not a local document."

---

## 7. Interaction — `CaseGraphRenderer`

`CaseGraphRenderer` (defined in `static/js/dashboard.js:1-174`) manages the SVG viewport:

| Action | Behaviour |
|---|---|
| Drag (left-click) | Pan; cursor `grabbing` |
| Ctrl/Cmd + scroll | Zoom in/out, clamped 0.1–5.0×, mouse-relative origin |
| `f` key | `fitToView()` — recalculates scale and offset to fit all nodes with padding |
| `c` key | `centerCritical()` — zooms to 1.2× and pans to center the first critical node |
| Node click | Opens Document HUD via `caseDashboard.selectDoc(id)` |
| Node hover | `setHighlight(id)` — dims non-adjacent nodes and fades non-incident edges |
| Node right-click | Context menu: View / Add reaction (stub) / Copy link (stub) |

The legend viewport (`#legend-viewport`) counteracts the pan/zoom transform so it stays anchored at top-right regardless of zoom level.

---

## 8. Empty and sparse states

| Situation | What renders |
|---|---|
| Case with no documents | Empty SVG with lane headers; `edge_count == 0` → auto-fallback to Timeline (see §1) |
| Case with 1 document | Single node, no edges; auto-fallback to Timeline applies |
| All nodes hidden by significance filter | Hidden-tier strip shows count per tier; `[show all]` chip resets filter |
| No documents in one lane | That lane collapses to zero width |
| Relationship detection pending | Graph renders nodes without edges; arrows appear after enrichment Celery task |
| Cross-proceeding ghost | Ghost node at lane boundary; no edges extend out of the current proceeding SVG |

---

## 9. Keyboard-first interaction

| Key | Scope | Action | Source |
|---|---|---|---|
| `g` | Dashboard | Switch to Graph view | `dashboard.js:375` |
| `f` | Graph active | Fit graph to viewport | `CaseGraphRenderer.fitToView()` |
| `c` | Graph active | Center + zoom to first critical node | `CaseGraphRenderer.centerCritical()` |
| `t` | Dashboard | Switch to Truth Map view | `dashboard.js:375` |
| `l` | Dashboard | Switch to Timeline view | `dashboard.js:374` |
| `$` | Dashboard | Switch to Financials view | `dashboard.js:375` |
| `/` | Dashboard | Open case chat (no HUD) or doc chat (HUD open) | `dashboard.js` |
| `Esc` | Any | Close HUD / context menu / chat | `dashboard.js` |

---

## 10. Data sources map

| Zone | Table | Phase |
|---|---|---|
| Documents (nodes) | `documents` (filtered by proceeding + significance) | Phase 1 |
| Relationships (edges) | `document_relationships` | Phase 4 (AI detection) |
| Lane assignment | `documents.originator_type` + `documents.attributed_originator` | Phase 3 (triage) |
| Reaction overlays | `user_reactions` (doc-scoped) | Phase 2 (triage) |
| Proof badges | `document_relationships` (ATTACHES_AS_PROOF count) | Phase 4 |
| Bundle grouping | `documents.court_relay` + `documents.role` + `ingest_batch_id` | Phase 3 |
| Per-proceeding scope | `proceedings.id` (active proceeding FK) | Phase 1 |

---

## 11. Files that will change

This spec documents the current implementation without requiring code changes. The two ⚠ items are accepted deviations:

- **Context menu stubs**: "Add reaction" and "Copy link" remain stubs for v1. When implemented, they will POST to `/document/{id}/reaction` and write `document://{id}` to the clipboard.
- **`SUPERSEDES` stroke weight**: accepted as-is for v1; if usability feedback surfaces, increase stroke-w to 0.75 in `correspondence_graph.html`.

**Modified (cross-reference only):**
- `docs/specs/02_dashboard.md §3` — replace inline graph description with one-paragraph summary + link to this spec.
- `docs/specs/00_vision.md §2` — add "See `docs/specs/03_correspondence_graph.md`" link.

**New:** none.

**Deleted:** none.

---

## 12. Phase progression

| Phase | What landed |
|---|---|
| Phase 1 | `documents` + `proceedings` + `DocumentRelationship` schema |
| Phase 3 | Triage assignments (`originator_type`, `attributed_originator`, `court_relay`) |
| Phase 4 | AI relationship detection (`replies_to`, `references`, `attaches_as_proof`, `supersedes`) |
| Phase 8 | `CaseGraphService` full implementation + SVG template + `CaseGraphRenderer` JS class |

---

## 13. Non-goals

- No force-directed layout (deterministic swim-lane positioning is intentional; force-directed would obscure the structural meaning of lanes).
- No d3.js or other graph library (the current SVG renderer is self-contained at ~282 LoC).
- No cross-case graph rollup (graphs are scoped to a single proceeding).
- No graph export to SVG/PNG (timeline on demand via browser print).
- No graph editing UI (relationships are AI-detected or inferred from ingest metadata; manual edge creation is out of scope for v1).
- No per-lane filtering (significance filter applies globally across all lanes).
- No animated transitions between filter states (nodes appear/disappear instantly on filter change).

---

## 14. Verification

**Manual:**
1. `make seed && make run` → open `/cases/<seeded-case>` → graph renders with ≥ 3 nodes in correct lanes; relationship arrows visible.
2. Top-bar filter `[critical]` → only critical nodes remain; hidden-tier strip shows count; `[all]` → all nodes reappear.
3. Click `f` → graph fits to viewport. Click `c` → first critical node centered with 1.2× zoom.
4. Click a node → Document HUD slides in from right.
5. Hover a node → incident edges highlighted; non-adjacent edges dimmed.
6. Right-click a node → context menu appears with three items.
7. Open a case with 0 edges (no relationships detected) → dashboard lands on Timeline view.
8. Bundle node with `court_relay=True` → click to expand; children render inside; click again to collapse.

**Automated (existing):**
- `tests/integration/test_case_graph_service.py` (if present — verify before running)
- `pytest -k graph` for any existing graph-related tests

---

## 15. Success criteria

- Graph renders in < 300 ms for cases with up to 200 nodes (SSR is synchronous; no client-side data fetch).
- `edge_count == 0` on first visit → dashboard auto-selects Timeline view (no empty graph flash).
- Lane assignment is deterministic: the same document always appears in the same lane regardless of render order.
- Significance filter round-trip (`significant+` → `all` → `critical` → `significant+`) leaves graph in correct state.
- Node hover highlights only the hovered node and its direct edges; no false highlighting.
- Bundle collapse/expand does not shift unrelated nodes (SVG height adjusts but lane column positions stay fixed).

---

## Related docs

- `docs/specs/00_vision.md` §2 — North star for structural layer
- `docs/specs/02_dashboard.md` — Case dashboard integration
- `docs/specs/04_document_hud.md` — Document HUD opened on node click
- `docs/specs/06_truth_map.md` — Factual layer (claims with evidence chain)
- `docs/specs/09_timeline.md` — Timeline fallback for sparse graphs

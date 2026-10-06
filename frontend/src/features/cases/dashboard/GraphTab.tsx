import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'

import {
  type CaseDetail,
  type GraphView,
  type SignificanceFilter,
  useCaseGraph,
} from '../../../api/caseDetail'
import { Button } from '../../../ui/Button'
import { Chip } from '../../../ui/Chip'
import { Icon } from '../../../ui/Icon'
import { QueryState } from '../../../ui/QueryState'
import { DocumentReview } from '../../documents/DocumentReview'

const LANE_COLOR: Record<string, string> = {
  own: 'var(--success)',
  court: 'var(--info)',
  opposing: 'var(--danger)',
  third: 'var(--warning)',
  third_party: 'var(--warning)',
  unknown: 'var(--line3)',
}
const laneColor = (key: string) => LANE_COLOR[key] ?? 'var(--line3)'

const CHILD_ROW_H = 48

/** Enter or Space on a focusable SVG group. */
function activate(e: React.KeyboardEvent, run: () => void) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    run()
  }
}
const REACTION_GLYPH: Record<string, string> = {
  lies: '🚩',
  true: '✅',
  needs_proof: '🔍',
  precedent: '⚖️',
}

type Props = {
  detail: CaseDetail
  selectedDoc: number | null
  onOpen: (id: number) => void
}

/** The correspondence swim-lane graph (layout from the server, interaction here). */
export function GraphTab({ detail, selectedDoc, onOpen }: Props) {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const filterParam = params.get('filter')
  const filter: SignificanceFilter =
    filterParam === 'critical' || filterParam === 'all' ? filterParam : 'significant+'
  const setFilter = (f: SignificanceFilter) => {
    params.set('filter', f)
    setParams(params, { replace: true })
  }
  const [hover, setHover] = useState<number | null>(null)
  const [laneFilter, setLaneFilter] = useState<string | null>(null)
  const [panelDoc, setPanelDoc] = useState<number | null>(null)
  const query = useCaseGraph(detail.id, detail.active_proceeding_id, filter, detail.last_visit)
  const graph = query.data

  const hidden = useMemo(() => {
    if (!graph) return 0
    const c = graph.node_counts
    if (filter === 'all') return 0
    if (filter === 'critical')
      return (
        (c.significant ?? 0) +
        (c.informational ?? 0) +
        (c.administrative_standalone ?? 0) +
        (c.administrative_relay ?? 0)
      )
    return c.administrative_standalone ?? 0
  }, [graph, filter])

  if (detail.active_proceeding_id === null) {
    return (
      <p className="p-6 text-[12px] text-muted">
        Create a proceeding to see the correspondence graph.
      </p>
    )
  }
  if (!graph) return <QueryState error={query.error} pending={query.isPending} />

  const open = (id: number) => {
    setPanelDoc(id)
    onOpen(id)
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-line px-4 py-2 text-[11px]">
        {(['critical', 'significant+', 'all'] as const).map((f) => (
          <Chip key={f} active={filter === f} onClick={() => setFilter(f)}>
            {f === 'critical' ? 'Critical' : f === 'significant+' ? 'Significant+' : 'All'}
          </Chip>
        ))}
        {hidden > 0 && (
          <button
            type="button"
            onClick={() => setFilter('all')}
            className="text-muted hover:text-ink"
          >
            {hidden} hidden by filter · show all
          </button>
        )}
        <span className="ml-auto font-mono text-muted">
          {graph.node_count} nodes · {graph.edge_count} edges
        </span>
        {graph.lanes.map((l) => (
          <button
            key={l.key}
            type="button"
            aria-pressed={laneFilter === l.key}
            onClick={() => setLaneFilter((v) => (v === l.key ? null : l.key))}
            className={`flex items-center gap-1 rounded-full border px-2 py-0.5 ${laneFilter === l.key ? 'border-accent text-ink' : 'border-line text-muted'}`}
          >
            <span className="h-2 w-2 rounded-full" style={{ background: laneColor(l.color) }} />
            {l.label}
          </button>
        ))}
      </div>
      <div className="relative flex min-h-0 flex-1">
        <Canvas
          graph={graph}
          hover={hover}
          setHover={setHover}
          laneFilter={laneFilter}
          selected={selectedDoc}
          onOpen={open}
        />
        {panelDoc !== null && (
          <aside
            aria-label="Document"
            className="absolute inset-y-0 right-0 z-20 w-[460px] overflow-y-auto border-l border-line bg-panel p-4 shadow-[-8px_0_32px_rgba(0,0,0,.35)]"
          >
            <div className="mb-2 flex items-center gap-2">
              <Button
                variant="secondary"
                className="px-2 py-1 text-[11px]"
                onClick={() => navigate(`/document/${panelDoc}`)}
              >
                <Icon name="open_in_full" size={13} /> Open HUD
              </Button>
              <button
                type="button"
                aria-label="Close document"
                onClick={() => setPanelDoc(null)}
                className="ml-auto text-muted hover:text-ink"
              >
                <Icon name="close" size={16} />
              </button>
            </div>
            <DocumentReview docId={panelDoc} onOpenHud={(id) => navigate(`/document/${id}`)} />
          </aside>
        )}
      </div>
    </div>
  )
}

function Canvas({
  graph,
  hover,
  setHover,
  laneFilter,
  selected,
  onOpen,
}: {
  graph: GraphView
  hover: number | null
  setHover: (id: number | null) => void
  laneFilter: string | null
  selected: number | null
  onOpen: (id: number) => void
}) {
  const viewport = useRef<HTMLDivElement>(null)
  const [camera, setCamera] = useState({ scale: 1, tx: 0, ty: 0 })
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null)

  // Fit the graph to the viewport width once per layout.
  useEffect(() => {
    const el = viewport.current
    if (!el) return
    const scale = Math.min(1.1, Math.max(0.6, (el.clientWidth - 24) / Math.max(graph.svg_width, 1)))
    // Rows run oldest → newest; open on the newest correspondence.
    const ty = Math.min(12, el.clientHeight - graph.svg_height * scale - 12)
    setCamera({ scale, tx: 12, ty })
  }, [graph.svg_width, graph.svg_height])

  useEffect(() => {
    const el = viewport.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const px = e.clientX - rect.left
      const py = e.clientY - rect.top
      setCamera((c) => {
        const scale = Math.min(5, Math.max(0.1, c.scale * (e.deltaY < 0 ? 1.1 : 0.9)))
        const k = scale / c.scale
        return { scale, tx: px - (px - c.tx) * k, ty: py - (py - c.ty) * k }
      })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  const neighbours = useMemo(() => {
    if (hover === null) return new Set<number>()
    const s = new Set<number>([hover])
    for (const e of graph.edges) {
      if (e.from_node_id === hover) s.add(e.to_node_id)
      if (e.to_node_id === hover) s.add(e.from_node_id)
    }
    return s
  }, [hover, graph.edges])

  const dim = (lane: string, id: number) =>
    (laneFilter !== null && lane !== laneFilter) || (hover !== null && !neighbours.has(id))

  return (
    <div
      ref={viewport}
      data-testid="graph-viewport"
      className="relative min-h-0 flex-1 cursor-grab overflow-hidden bg-[radial-gradient(900px_400px_at_55%_0%,var(--panel2),var(--card2))] active:cursor-grabbing"
      onMouseDown={(e) => {
        if ((e.target as HTMLElement).closest('[data-node]')) return
        drag.current = { x: e.clientX, y: e.clientY, tx: camera.tx, ty: camera.ty }
      }}
      onMouseMove={(e) => {
        const d = drag.current
        if (!d) return
        setCamera((c) => ({ ...c, tx: d.tx + (e.clientX - d.x), ty: d.ty + (e.clientY - d.y) }))
      }}
      onMouseUp={() => (drag.current = null)}
      onMouseLeave={() => (drag.current = null)}
    >
      <div className="pointer-events-none absolute top-0 right-0 left-0 z-10 flex bg-gradient-to-b from-card2 via-card2/80 to-transparent px-2 pt-2 pb-5">
        {graph.lanes.map((l) => (
          <div
            key={l.key}
            className="flex items-center justify-center gap-2 text-[11px] font-semibold tracking-[.12em] uppercase"
            style={{
              width: 280 * camera.scale,
              marginLeft: l === graph.lanes[0] ? 36 * camera.scale + camera.tx : 0,
            }}
          >
            <span className="h-2 w-2 rounded-full" style={{ background: laneColor(l.color) }} />
            <span style={{ color: laneColor(l.color) }}>{l.label}</span>
          </div>
        ))}
      </div>
      <svg
        width="100%"
        height="100%"
        style={{ display: 'block' }}
        aria-label="Correspondence graph"
      >
        <g transform={`translate(${camera.tx} ${camera.ty}) scale(${camera.scale})`}>
          <DateAxis graph={graph} />
          {graph.edges.map((e) => {
            const lit = hover !== null && (e.from_node_id === hover || e.to_node_id === hover)
            return (
              <path
                key={e.id}
                d={e.path}
                fill="none"
                stroke={lit ? 'var(--accent)' : 'var(--line3)'}
                strokeWidth={lit ? e.stroke_w + 1 : e.stroke_w}
                strokeDasharray={e.dasharray ?? undefined}
                opacity={hover !== null && !lit ? 0.25 : 0.9}
                markerEnd={e.arrow ? 'url(#arrow)' : undefined}
              />
            )
          })}
          <defs>
            <marker
              id="arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--line3)" />
            </marker>
          </defs>
          {graph.bundles.map((b) => {
            const h = 16 + b.children.length * CHILD_ROW_H + 16
            return (
              <g
                key={`b-${b.id}`}
                data-node
                role="button"
                tabIndex={0}
                aria-label={`${b.header}: ${b.children.length} documents`}
                opacity={dim(b.lane, b.id) ? 0.3 : 1}
                onMouseEnter={() => setHover(b.id)}
                onMouseLeave={() => setHover(null)}
                onFocus={() => setHover(b.id)}
                onBlur={() => setHover(null)}
                className="cursor-pointer outline-none focus-visible:[&>rect:first-child]:stroke-accent"
                onClick={() => onOpen(b.id)}
                onKeyDown={(e) => activate(e, () => onOpen(b.id))}
              >
                <rect
                  x={b.x - 8}
                  y={b.y - 8}
                  width={195}
                  height={h + 16}
                  rx={10}
                  fill="var(--card)"
                  stroke="var(--warning)"
                  strokeDasharray="3 3"
                />
                <rect
                  x={b.x - 8}
                  y={b.y - 8}
                  width={5}
                  height={h + 16}
                  rx={10}
                  fill={laneColor(b.originator)}
                />
                <text
                  x={b.x + 10}
                  y={b.y + 10}
                  fontSize={12}
                  fontWeight={700}
                  fill="var(--warning)"
                  fontFamily="var(--font-display)"
                >
                  {b.header}
                </text>
                {b.children.map((c, i) => {
                  const cy = b.y + 16 + i * CHILD_ROW_H
                  return (
                    <g
                      key={c.id}
                      data-node
                      role="button"
                      tabIndex={0}
                      aria-label={c.title}
                      className="cursor-pointer outline-none"
                      onClick={(e) => {
                        e.stopPropagation()
                        onOpen(c.id)
                      }}
                      onKeyDown={(e) => {
                        e.stopPropagation()
                        activate(e, () => onOpen(c.id))
                      }}
                    >
                      <rect
                        x={b.x + 2}
                        y={cy}
                        width={175}
                        height={CHILD_ROW_H - 4}
                        rx={3}
                        fill="var(--card2)"
                        stroke={selected === c.id ? 'var(--accent)' : 'var(--line2)'}
                        strokeWidth={selected === c.id ? 1.5 : 0.5}
                      />
                      <rect
                        x={b.x + 2}
                        y={cy}
                        width={4}
                        height={CHILD_ROW_H - 4}
                        fill={laneColor(c.origin)}
                      />
                      <text
                        x={b.x + 13}
                        y={cy + 16}
                        fontSize={12}
                        fontWeight={600}
                        fill="var(--ink)"
                      >
                        {c.title}
                      </text>
                      <text
                        x={b.x + 13}
                        y={cy + 32}
                        fontSize={10.5}
                        fill="var(--muted)"
                        fontFamily="var(--font-mono)"
                      >
                        {c.date} · {(c.tier ?? '').slice(0, 3)}
                      </text>
                    </g>
                  )
                })}
                {b.footer && (
                  <text
                    x={b.x + 10}
                    y={b.y + h - 4}
                    fontSize={10.5}
                    fill="var(--muted)"
                    fontFamily="var(--font-mono)"
                  >
                    {b.footer}
                  </text>
                )}
              </g>
            )
          })}
          {graph.nodes
            .filter((n) => !n.is_bundle)
            .map((n) => (
              <g
                key={n.id}
                data-node
                data-id={n.id}
                opacity={dim(n.lane, n.id) ? 0.3 : n.ghost ? 0.55 : 1}
                onMouseEnter={() => setHover(n.id)}
                onMouseLeave={() => setHover(null)}
                onClick={() => onOpen(n.id)}
                className="cursor-pointer"
                role="button"
                aria-label={n.full_title}
              >
                <rect
                  x={n.x}
                  y={n.y}
                  width={n.w}
                  height={n.h}
                  rx={8}
                  fill={n.ghost ? 'transparent' : 'var(--card)'}
                  stroke={
                    selected === n.id
                      ? 'var(--accent)'
                      : n.thread_open
                        ? 'var(--warning)'
                        : n.is_new_since_last_visit
                          ? 'var(--info)'
                          : 'var(--line2)'
                  }
                  strokeWidth={selected === n.id || n.thread_open ? 1.5 : 1}
                  strokeDasharray={n.ghost ? '4 3' : undefined}
                />
                <rect
                  x={n.x}
                  y={n.y}
                  width={3}
                  height={n.h}
                  rx={2}
                  fill={laneColor(n.originator_type === 'third_party' ? 'third' : n.lane)}
                />
                <text x={n.x + 10} y={n.y + 19} fontSize={12} fontWeight={600} fill="var(--ink)">
                  {n.tier === 'critical' ? '⚑ ' : ''}
                  {n.title}
                </text>
                <text
                  x={n.x + 10}
                  y={n.y + 36}
                  fontSize={10.5}
                  fill="var(--muted)"
                  fontFamily="var(--font-mono)"
                >
                  {n.date_short}
                  {n.cross_proceeding && n.proceeding_label ? ` · ${n.proceeding_label}` : ''}
                  {n.tier ? ` · ${n.tier.slice(0, 4)}` : ''}
                </text>
                {graph.proof_badges[String(n.id)] && (
                  <text
                    x={n.x + n.w - 8}
                    y={n.y + 14}
                    fontSize={10}
                    textAnchor="end"
                    fill="var(--success)"
                  >
                    ⫸{graph.proof_badges[String(n.id)]}
                  </text>
                )}
                {n.reaction && (
                  <text x={n.x + n.w - 8} y={n.y + n.h - 8} fontSize={11} textAnchor="end">
                    {REACTION_GLYPH[n.reaction] ?? ''}
                  </text>
                )}
              </g>
            ))}
        </g>
      </svg>
      <p className="pointer-events-none absolute bottom-2 left-3 flex items-center gap-1 text-[10px] text-muted2">
        <Icon name="touch_app" size={12} /> drag to pan · ⌘/Ctrl + wheel to zoom
      </p>
    </div>
  )
}

function DateAxis({ graph }: { graph: GraphView }) {
  let prev: string | null = null
  const ticks: { y: number; label: string }[] = []
  for (const n of graph.nodes) {
    if (n.date_short !== prev) ticks.push({ y: n.y + 30, label: n.date_short })
    prev = n.date_short
  }
  return (
    <g>
      {ticks.map((t) => (
        <text
          key={`${t.y}-${t.label}`}
          x={4}
          y={t.y}
          fontSize={11}
          fill="var(--muted)"
          fontFamily="var(--font-mono)"
          opacity={0.7}
        >
          {t.label}
        </text>
      ))}
    </g>
  )
}

// Correspondence Graph — swim-lane SVG with nodes, edges, bundle relay
const { useState, useMemo, useRef, useEffect } = React;

function originatorColor(key) {
  const map = {
    court: "var(--originator-court)",
    opposing: "var(--originator-opposing)",
    own: "var(--originator-own)",
    third: "var(--originator-third)",
    unknown: "var(--originator-unknown)",
  };
  return map[key] || map.unknown;
}

function CorrespondenceGraph({ filter, onSelect, selectedId, highlightIds, hiddenAdmin = true }) {
  const LANES = window.LANES;
  const DOCUMENTS = window.DOCUMENTS;
  const EDGES = window.EDGES;

  // Layout constants
  const LANE_W = 180;
  const ROW_H = 64;
  const TOP = 64;
  const LEFT = 36;
  const NODE_W = 144;
  const NODE_H = 40;

  const laneIndex = Object.fromEntries(LANES.map((l, i) => [l.key, i]));

  // Significance filter
  const nodes = useMemo(() => {
    const keep = DOCUMENTS.filter(d => {
      if (filter === "critical") return d.sig === "critical";
      if (filter === "significant+") return d.sig !== "administrative" || d.role === "relay";
      return true;
    });
    return keep.map(d => ({
      ...d,
      x: LEFT + laneIndex[d.lane] * LANE_W + (LANE_W - NODE_W) / 2,
      y: TOP + d.row * ROW_H,
    }));
  }, [filter]);

  const nodeById = Object.fromEntries(nodes.map(n => [n.id, n]));
  const hiddenCount = DOCUMENTS.length - nodes.length;

  // Bundle child rendering (russian doll)
  const bundle = nodeById["bundle"];
  const bundleChildren = bundle ? DOCUMENTS.find(d => d.id === "bundle").children : [];

  const totalRows = Math.max(...nodes.map(n => n.row)) + 1;
  const height = TOP + totalRows * ROW_H + 120;
  const width = LEFT * 2 + LANES.length * LANE_W;

  // Edges
  const renderEdge = (e) => {
    const a = nodeById[e.from], b = nodeById[e.to];
    if (!a || !b) return null;
    const ax = a.x + NODE_W / 2;
    const ay = a.y + NODE_H / 2;
    const bx = b.x + NODE_W / 2;
    const by = b.y + NODE_H / 2;

    // routing: vertical from a, curve to b
    const dx = bx - ax;
    const sameLane = Math.abs(dx) < 2;
    let path;
    if (sameLane) {
      path = `M ${ax} ${ay + NODE_H/2} L ${bx} ${by - NODE_H/2}`;
    } else {
      const mx = ax + dx / 2;
      path = `M ${ax + (dx>0?NODE_W/2:-NODE_W/2)} ${ay}
              C ${mx} ${ay}, ${mx} ${by}, ${bx + (dx>0?-NODE_W/2:NODE_W/2)} ${by}`;
    }
    const stroke = e.kind === "relay" ? "var(--amber)" : "var(--outline-variant)";
    const strokeW = e.kind === "relay" || e.kind === "requires" ? 1.5 : 1;
    const dash = e.kind === "requires" ? "4 3" : "";
    return (
      <path key={`${e.from}-${e.to}`} d={path} stroke={stroke} strokeWidth={strokeW}
            strokeDasharray={dash} fill="none" opacity={0.75}
            markerEnd={e.kind === "ack" ? "" : "url(#arrow)"} />
    );
  };

  return (
    <div className="relative h-full overflow-auto custom-scrollbar" style={{ background: "var(--surface)" }}>
      {/* Lane headers (sticky) */}
      <div className="sticky top-0 z-10 flex"
           style={{ background: "linear-gradient(to bottom, var(--surface) 75%, transparent)", paddingLeft: LEFT, paddingTop: 14, paddingBottom: 10 }}>
        {LANES.map(l => (
          <div key={l.key} style={{ width: LANE_W }} className="flex items-center gap-2">
            <span className="inline-block h-2 w-2 rounded-full" style={{ background: originatorColor(l.color) }} />
            <span className="font-headline text-[10.5px] font-bold tracking-[0.12em]"
                  style={{ color: "var(--on-surface-variant)" }}>
              {l.label}
            </span>
          </div>
        ))}
      </div>

      <svg width={width} height={height} style={{ display: "block" }}>
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5"
                  markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--outline)" />
          </marker>
          <pattern id="lane-grid" width={LANE_W} height={ROW_H} patternUnits="userSpaceOnUse">
            <path d={`M 0 ${ROW_H} L ${LANE_W} ${ROW_H}`} stroke="var(--outline-variant)" strokeWidth="0.5" opacity="0.25" />
          </pattern>
        </defs>

        {/* Lane separators */}
        {LANES.map((l, i) => (
          <line key={`lsep${i}`}
                x1={LEFT + i * LANE_W} y1={TOP - 8}
                x2={LEFT + i * LANE_W} y2={height - 80}
                stroke="var(--outline-variant)" strokeWidth="0.5" strokeDasharray="2 6" opacity="0.4" />
        ))}
        <line x1={LEFT + LANES.length * LANE_W} y1={TOP - 8}
              x2={LEFT + LANES.length * LANE_W} y2={height - 80}
              stroke="var(--outline-variant)" strokeWidth="0.5" strokeDasharray="2 6" opacity="0.4" />

        {/* Date axis tick rows (subtle) */}
        {nodes.map(n => (
          <text key={`dt-${n.id}`} x={8} y={n.y + NODE_H/2 + 4}
                className="font-mono" fontSize="10" fill="var(--on-surface-variant)" opacity="0.6">
            {n.date.replace(/^2026-/,"")}
          </text>
        ))}

        {/* Edges */}
        <g>{EDGES.map(renderEdge)}</g>

        {/* Nodes */}
        {nodes.map(n => {
          if (n.id === "bundle") return <BundleNode key={n.id} n={n} children={bundleChildren} onSelect={onSelect} selected={selectedId===n.id} />;
          return <DocNode key={n.id} n={n} onSelect={onSelect} selected={selectedId===n.id} highlight={highlightIds?.includes(n.id)} />;
        })}
      </svg>

      {/* Hidden-tier indicator */}
      <div className="absolute bottom-3 left-10 flex items-center gap-2 text-[11px]"
           style={{ color: "var(--on-surface-variant)" }}>
        <Icon name="info" size={14}/>
        <span>{hiddenCount} administrative document{hiddenCount!==1?"s":""} hidden</span>
        <button className="ml-1 underline-offset-2 hover:underline" style={{ color: "var(--primary)" }}>show all</button>
      </div>
    </div>
  );
}

function DocNode({ n, onSelect, selected, highlight }) {
  const NODE_W = 144, NODE_H = 40;
  const stroke = originatorColor(n.lane === "court" ? "court" : n.lane);
  const fill = n.ghost ? "transparent" : "var(--surface-container)";
  const isThread = n.thread;

  return (
    <g className={`graph-node ${selected ? "selected" : ""}`} onClick={() => onSelect(n.id)}>
      {/* amber thread-open glow */}
      {isThread && (
        <rect x={n.x - 3} y={n.y - 3} width={NODE_W + 6} height={NODE_H + 6} rx="7"
              fill="none" stroke="var(--amber)" strokeWidth="1" opacity="0.5"
              strokeDasharray={n.ghost ? "4 3" : ""} />
      )}
      {/* card */}
      <rect x={n.x} y={n.y} width={NODE_W} height={NODE_H} rx="5"
            fill={fill}
            stroke={selected ? "var(--primary)" : "var(--outline-variant)"}
            strokeWidth={selected ? 1.5 : 1}
            strokeDasharray={n.ghost ? "3 3" : ""} />
      {/* originator stripe (left border, 4px) */}
      <rect x={n.x} y={n.y} width="4" height={NODE_H} rx="5" fill={stroke} />
      {/* significance flag */}
      {n.sig === "critical" && (
        <text x={n.x + NODE_W - 14} y={n.y + 14} fontSize="11" fill="var(--critical)" fontWeight="700">⚑</text>
      )}
      {highlight && (
        <rect x={n.x-1} y={n.y-1} width={NODE_W+2} height={NODE_H+2} rx="6"
              fill="none" stroke="var(--primary)" strokeWidth="1.5" opacity="0.8" />
      )}
      {/* title */}
      <text x={n.x + 12} y={n.y + 17} fontSize="11" fontWeight="600" fill="var(--on-surface)"
            style={{ fontFamily: "Inter" }}>
        {clip(n.title, 17)}
      </text>
      <text x={n.x + 12} y={n.y + 31} fontSize="10" fill="var(--on-surface-variant)" style={{ fontFamily: "Inter" }}>
        {n.role}
      </text>
    </g>
  );
}

function BundleNode({ n, children, onSelect, selected }) {
  const NODE_W = 144;
  // Expanded bundle: header + 2 children rows (court relay visual)
  const rowH = 26;
  const bh = 16 + children.length * rowH + 22;

  return (
    <g className={`graph-node ${selected ? "selected" : ""}`}>
      {/* backdrop — slightly larger, dashed amber = relay */}
      <rect x={n.x - 6} y={n.y - 6} width={NODE_W + 12} height={bh + 12} rx="8"
            fill="var(--surface-container)"
            stroke="var(--amber)" strokeWidth="1" strokeDasharray="3 3" opacity="0.9" />
      {/* court originator stripe */}
      <rect x={n.x - 6} y={n.y - 6} width="4" height={bh + 12} rx="8" fill="var(--originator-court)" />

      {/* Header */}
      <text x={n.x + 8} y={n.y + 8} fontSize="10" fill="var(--amber)" fontWeight="700"
            style={{ fontFamily: "Manrope", letterSpacing: "0.08em" }}>
        ⚑ COURT RELAY · Beglaubigung
      </text>

      {children.map((c, i) => {
        const cy = n.y + 16 + i * rowH;
        const col = originatorColor(c.origin);
        return (
          <g key={c.id} onClick={() => onSelect(c.id)} className="graph-node">
            <rect x={n.x + 2} y={cy} width={NODE_W - 4} height={rowH - 4} rx="3"
                  fill="var(--surface-lowest)" stroke="var(--outline-variant)" strokeWidth="0.5" />
            <rect x={n.x + 2} y={cy} width="3" height={rowH - 4} fill={col} />
            <text x={n.x + 10} y={cy + 14} fontSize="10.5" fontWeight="600" fill="var(--on-surface)">
              {c.title}
            </text>
          </g>
        );
      })}

      {/* Footer date */}
      <text x={n.x + 8} y={n.y + bh + 1} fontSize="9.5" fill="var(--on-surface-variant)"
            style={{ fontFamily: "JetBrains Mono" }}>
        zugestellt 12.03
      </text>
    </g>
  );
}

function clip(s, n) { return s.length > n ? s.slice(0,n-1) + "…" : s; }

Object.assign(window, { CorrespondenceGraph });

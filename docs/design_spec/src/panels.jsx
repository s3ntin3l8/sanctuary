// Left-column panels + Action items panel

function AIBriefPanel({ brief, updatedAt, onRefresh }) {
  return (
    <div className="p-4 pb-5">
      <div className="flex items-center justify-between mb-2">
        <h3 className="font-headline text-[10.5px] font-bold tracking-[0.14em]"
            style={{ color: "var(--on-surface-variant)" }}>AI BRIEF</h3>
        <button onClick={onRefresh}
                className="flex items-center gap-1 text-[10.5px] focus-ring"
                style={{ color: "var(--primary)" }}>
          <Icon name="refresh" size={12}/>
          refresh
        </button>
      </div>

      <p className="font-headline text-[15px] leading-snug mb-3" style={{ color: "var(--on-surface)" }}>
        {brief.status_line.split("Stellungnahme").map((part, i, arr) =>
          i < arr.length - 1
            ? <React.Fragment key={i}>{part}<span className="cite">Stellungnahme</span></React.Fragment>
            : <React.Fragment key={i}>{part}</React.Fragment>
        )}
      </p>

      <div className="mb-3">
        <div className="text-[10.5px] uppercase tracking-[0.1em] mb-1.5"
             style={{ color: "var(--on-surface-variant)" }}>Key risks</div>
        <ul className="space-y-1.5">
          {brief.key_risks.map(r => (
            <li key={r.id} className="flex items-start gap-2 text-[12.5px]">
              <span className="mt-[5px] h-1.5 w-1.5 rounded-full flex-shrink-0"
                    style={{ background: r.severity === "critical" ? "var(--critical)" : "var(--near)" }} />
              <span>
                <span style={{ color: "var(--on-surface)" }}>{r.label}</span>
                <span style={{ color: "var(--on-surface-variant)" }}> — {r.sub}</span>
              </span>
            </li>
          ))}
        </ul>
      </div>

      <div className="mb-3">
        <div className="text-[10.5px] uppercase tracking-[0.1em] mb-1.5"
             style={{ color: "var(--on-surface-variant)" }}>Open threads</div>
        {brief.open_threads.map((t, i) => (
          <div key={i} className="text-[12.5px] flex items-start gap-2">
            <span className="mt-[5px] h-1.5 w-1.5 rounded-full flex-shrink-0 pulse-amber"
                  style={{ background: "var(--amber)" }} />
            <span>
              <span style={{ color: "var(--on-surface)" }}>{t.thread}</span>
              <span style={{ color: "var(--on-surface-variant)" }}> — {t.description}</span>
            </span>
          </div>
        ))}
      </div>

      <div>
        <div className="text-[10.5px] uppercase tracking-[0.1em] mb-1.5"
             style={{ color: "var(--on-surface-variant)" }}>Recent</div>
        <div className="text-[12.5px]" style={{ color: "var(--on-surface)" }}>
          {brief.recent_development}
        </div>
        <div className="mt-2 text-[10.5px] font-mono" style={{ color: "var(--on-surface-variant)" }}>
          updated {updatedAt}
        </div>
      </div>
    </div>
  );
}

function PartiesPanel({ parties, onPartyFilter, activeFilter }) {
  const colorFor = k => ({
    court: "var(--originator-court)",
    opposing: "var(--originator-opposing)",
    own: "var(--originator-own)",
    third: "var(--originator-third)",
  }[k]);
  return (
    <div className="p-4 pb-5">
      <h3 className="font-headline text-[10.5px] font-bold tracking-[0.14em] mb-2"
          style={{ color: "var(--on-surface-variant)" }}>PARTIES</h3>
      <ul className="space-y-1.5">
        {parties.map(p => {
          const on = activeFilter === p.color;
          return (
            <li key={p.key}>
              <button onClick={() => onPartyFilter(p.color)}
                      className="w-full flex items-center gap-2.5 px-2 py-1 -mx-2 rounded row-hover text-left"
                      style={ on ? { background: "var(--surface-high)" } : {} }>
                <span className="h-2 w-2 rounded-full flex-shrink-0"
                      style={{ background: colorFor(p.color) }} />
                <span className="text-[12.5px]" style={{ color: "var(--on-surface)" }}>{p.label}</span>
                <span className="text-[11.5px] ml-auto truncate"
                      style={{ color: "var(--on-surface-variant)" }}>{p.name}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function FinancialsPanel({ financials, onBreakdown }) {
  return (
    <div className="p-4 pb-5">
      <h3 className="font-headline text-[10.5px] font-bold tracking-[0.14em] mb-2"
          style={{ color: "var(--on-surface-variant)" }}>FINANCIALS</h3>
      <div className="flex items-baseline gap-2 mb-1">
        <span className="font-headline text-[22px] font-bold"
              style={{ color: "var(--on-surface)" }}>
          {financials.total_eur.toLocaleString("de-DE")} €
        </span>
        <span className="text-[11px] uppercase tracking-wide"
              style={{ color: "var(--on-surface-variant)" }}>total exposure</span>
      </div>
      <div className="text-[12px] mb-3" style={{ color: "var(--on-surface-variant)" }}>
        <span style={{ color: "var(--originator-own)" }}>
          +{financials.last_delta.amount} €
        </span>
        {" "}({financials.last_delta.date}) — {financials.last_delta.label}
      </div>

      <div className="rounded-md p-2.5 mb-2" style={{ background: "var(--surface-low)" }}>
        {financials.breakdown.map((b, i) => (
          <div key={i} className="flex items-center justify-between text-[11.5px] py-0.5">
            <span style={{ color: "var(--on-surface-variant)" }}>{b.label}</span>
            <span className="font-mono" style={{ color: "var(--on-surface)" }}>
              {b.amount} €
              <span className="ml-1.5 text-[9.5px] uppercase opacity-70">{b.status}</span>
            </span>
          </div>
        ))}
      </div>

      <button onClick={onBreakdown} className="flex items-center gap-1 text-[11.5px] focus-ring"
              style={{ color: "var(--primary)" }}>
        full breakdown
        <Icon name="arrow-right" size={12}/>
      </button>
    </div>
  );
}

function ActionItemsPanel({ items, filter, setFilter, onOpenDoc }) {
  const urgencyGlyph = {
    critical: <span style={{ color: "var(--critical)" }}>⚑</span>,
    near:     <span style={{ color: "var(--near)" }}>·</span>,
    far:      <span style={{ color: "var(--on-surface-variant)" }}>◦</span>,
    clock:    <span style={{ color: "var(--on-surface-variant)" }}>◦</span>,
  };

  return (
    <div className="h-full flex flex-col" style={{ background: "var(--surface-low)" }}>
      <div className="px-5 py-3 flex items-center justify-between border-b"
           style={{ borderColor: "var(--outline-variant)" }}>
        <h3 className="font-headline text-[11px] font-bold tracking-[0.14em]"
            style={{ color: "var(--on-surface-variant)" }}>ACTION ITEMS</h3>
        <div className="flex items-center gap-1 text-[11px]">
          {["open","completed","all"].map(k => (
            <button key={k} onClick={() => setFilter(k)}
                    className="px-2 py-0.5 rounded"
                    style={ filter === k
                      ? { background: "var(--primary-container)", color: "var(--primary)" }
                      : { color: "var(--on-surface-variant)" } }>
              {k}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-auto custom-scrollbar">
        {items.map(it => (
          <div key={it.id}
               className="row-actionitem px-5 py-3 border-b row-hover flex items-start gap-3 cursor-pointer"
               style={{ borderColor: "var(--outline-variant)" }}
               onClick={() => it.sourceDocId && onOpenDoc(it.sourceDocId)}>
            <div className="pt-[2px] text-[14px] w-4 text-center">{urgencyGlyph[it.urgency]}</div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-3 mb-0.5">
                {it.date ? (
                  <>
                    <span className="font-mono text-[12.5px]" style={{ color: "var(--on-surface)" }}>{it.date}</span>
                    {it.days != null && (
                      <span className="font-mono text-[11px]"
                            style={{ color: it.urgency === "critical" ? "var(--critical)" : "var(--on-surface-variant)" }}>
                        {it.days}d
                      </span>
                    )}
                  </>
                ) : (
                  <span className="text-[11px] italic" style={{ color: "var(--on-surface-variant)" }}>
                    typical tempo
                  </span>
                )}
                <span className="chip ml-auto">{it.badge}</span>
              </div>
              <div className="text-[13px]" style={{ color: "var(--on-surface)" }}>{it.title}</div>
              {it.clock && (
                <div className="mt-1 text-[11.5px] pl-3 border-l-2 font-mono"
                     style={{ color: "var(--on-surface-variant)", borderColor: "var(--outline-variant)" }}>
                  → {it.clock}
                </div>
              )}
            </div>
          </div>
        ))}

        {/* Dormancy alert */}
        <div className="m-5 p-3 rounded-md flex items-start gap-2.5"
             style={{ background: "var(--surface-container)", border: "1px dashed var(--outline-variant)" }}>
          <Icon name="hourglass" size={16} color="var(--amber)"/>
          <div className="text-[11.5px]" style={{ color: "var(--on-surface-variant)" }}>
            This proceeding has been quiet 6 months — longer than typical.
            Is something pending outside the system?
            <div className="mt-1.5 flex gap-3 text-[11px]">
              <button style={{ color: "var(--primary)" }}>add note</button>
              <button style={{ color: "var(--on-surface-variant)" }}>dismiss</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { AIBriefPanel, PartiesPanel, FinancialsPanel, ActionItemsPanel });

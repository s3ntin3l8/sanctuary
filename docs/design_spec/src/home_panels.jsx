// Home page panels — Today, Triage, Delta, Signals, Active Cases

const { useState: useHomeState, useRef: useHomeRef } = React;

// ——— small shared components ———

function PanelHeader({ icon, title, meta, action }) {
  return (
    <div className="flex items-baseline justify-between mb-3">
      <div className="flex items-center gap-2">
        <Icon name={icon} size={14} color="var(--on-surface-variant)" />
        <h2 className="font-headline text-[12px] font-semibold tracking-widest uppercase"
            style={{ color: "var(--on-surface)" }}>
          {title}
        </h2>
        {meta && (
          <span className="text-[11px]" style={{ color: "var(--on-surface-variant)" }}>
            {meta}
          </span>
        )}
      </div>
      {action && (
        <button className="text-[11px] flex items-center gap-1 focus-ring"
                style={{ color: "var(--primary)" }}>
          {action} <Icon name="arrow-right" size={12} color="var(--primary)"/>
        </button>
      )}
    </div>
  );
}

function CaseChip({ id, onClick }) {
  return (
    <button onClick={onClick}
            className="font-mono text-[11px] px-1.5 py-0.5 rounded focus-ring"
            style={{
              color: "var(--on-surface)",
              background: "var(--surface-container)",
              border: "1px solid var(--outline-variant)",
            }}>
      {id}
    </button>
  );
}

function Panel({ children, className = "", focused = false }) {
  return (
    <section className={`rounded-lg p-5 ${className}`}
             style={{
               background: "var(--surface-lowest)",
               border: `1px solid ${focused ? "var(--primary)" : "var(--outline-variant)"}`,
               boxShadow: focused ? "0 0 0 3px rgba(87,241,219,0.06)" : "none",
               transition: "border-color .15s, box-shadow .15s",
             }}>
      {children}
    </section>
  );
}

// ——— 1. TODAY panel ———

function TodayPanel({ items, focused, focusedIdx }) {
  if (items.length === 0) {
    return (
      <Panel focused={focused}>
        <PanelHeader icon="gavel" title="Today" />
        <div className="text-[13px] py-2" style={{ color: "var(--on-surface-variant)" }}>
          No deadlines in the next 30 days.
        </div>
      </Panel>
    );
  }

  return (
    <Panel focused={focused}>
      <PanelHeader icon="gavel" title="Today" meta={`${items.length} items`} />
      <div className="space-y-0">
        {items.map((it, i) => {
          const isFocused = focused && i === focusedIdx;
          return (
            <div key={it.id}
                 className="flex items-center gap-3 py-2 px-2 -mx-2 rounded row-hover cursor-pointer"
                 style={{
                   background: isFocused ? "var(--surface-low)" : "transparent",
                   outline: isFocused ? "1px solid var(--outline-variant)" : "none",
                 }}>
              {/* urgency icon */}
              <div className="w-5 flex justify-center">
                {it.urgency === "critical" ? (
                  <span style={{ color: "var(--critical)", fontSize: 14, lineHeight: 1 }}>⚑</span>
                ) : (
                  <span style={{ color: "var(--on-surface-variant)", fontSize: 14, lineHeight: 1 }}>·</span>
                )}
              </div>
              {/* date column */}
              <div className="font-mono text-[11px] w-[98px] flex-shrink-0"
                   style={{ color: it.urgency === "critical" ? "var(--critical)" : "var(--on-surface-variant)" }}>
                {formatDueDate(it.dueDate, it.daysUntil)}
              </div>
              {/* title + subtitle */}
              <div className="flex-1 min-w-0">
                <div className="text-[13px] truncate" style={{ color: "var(--on-surface)" }}>
                  {it.title}
                </div>
                <div className="text-[11px] truncate" style={{ color: "var(--on-surface-variant)" }}>
                  {it.subtitle}{it.amount ? ` · ${it.amount}` : ""}
                </div>
              </div>
              {/* case chip */}
              <CaseChip id={it.caseId} />
              {/* open */}
              <button className="flex items-center gap-1 text-[11px] focus-ring px-2 py-1 rounded"
                      style={{ color: "var(--primary)" }}>
                open <Icon name="arrow-right" size={11} color="var(--primary)"/>
              </button>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

function formatDueDate(iso, daysUntil) {
  const d = new Date(iso);
  const mm = d.toLocaleString("en", { month: "short" });
  const dd = String(d.getDate()).padStart(2, "0");
  const rel = daysUntil < 0 ? `${Math.abs(daysUntil)}d late`
            : daysUntil === 0 ? "today"
            : daysUntil === 1 ? "1d"
            : `${daysUntil}d`;
  return `${mm} ${dd} · ${rel}`;
}

// ——— 2. AWAITING TRIAGE panel ———

function TriagePanel({ bundles, focused, focusedIdx }) {
  if (bundles.length === 0) {
    return (
      <Panel focused={focused}>
        <PanelHeader icon="hub" title="Awaiting Triage" />
        <div className="text-[13px] py-2 flex items-center gap-2" style={{ color: "var(--on-surface-variant)" }}>
          <Icon name="check" size={13} color="var(--originator-own)" />
          Triage queue is clear.
        </div>
      </Panel>
    );
  }

  return (
    <Panel focused={focused}>
      <PanelHeader icon="hub" title="Awaiting Triage"
                   meta={`${bundles.length} bundle${bundles.length === 1 ? "" : "s"}`} />
      <div className="space-y-0">
        {bundles.map((b, i) => {
          const isFocused = focused && i === focusedIdx;
          return (
            <div key={b.id}
                 className="flex items-center gap-3 py-2 px-2 -mx-2 rounded row-hover cursor-pointer"
                 style={{
                   background: isFocused ? "var(--surface-low)" : "transparent",
                   outline: isFocused ? "1px solid var(--outline-variant)" : "none",
                 }}>
              <div className="w-5 flex justify-center">
                <span className="font-mono text-[11px] font-semibold"
                      style={{ color: "var(--amber)" }}>
                  {b.docCount}
                </span>
              </div>
              <div className="font-mono text-[11px] w-[50px] flex-shrink-0"
                   style={{ color: "var(--on-surface-variant)" }}>
                {b.receivedLabel}
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-[13px] truncate flex items-center gap-2" style={{ color: "var(--on-surface)" }}>
                  <span>{b.sourceHint}</span>
                  <Icon name="arrow-right" size={11} color="var(--on-surface-variant)" />
                  {b.suggestedCaseId ? (
                    <span className="flex items-center gap-1">
                      <span className="font-mono text-[11px]">{b.suggestedCaseId}</span>
                      {b.confidence === "suggested" && (
                        <span style={{ color: "var(--amber)" }}>?</span>
                      )}
                    </span>
                  ) : (
                    <span className="text-[11px]" style={{ color: "var(--amber)" }}>
                      no match yet
                    </span>
                  )}
                </div>
                <div className="text-[11px] truncate" style={{ color: "var(--on-surface-variant)" }}>
                  {b.preview}
                </div>
              </div>
              <button className="flex items-center gap-1 text-[11px] font-medium focus-ring px-2.5 py-1 rounded"
                      style={{
                        color: "var(--on-primary)",
                        background: "var(--primary)",
                      }}>
                triage <Icon name="arrow-right" size={11} color="var(--on-primary)"/>
              </button>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

// ——— 3. DELTA feed ———

function DeltaPanel({ cases, lastVisitLabel, focused, focusedIdx, onMarkReviewed }) {
  if (cases.length === 0) {
    return (
      <Panel focused={focused}>
        <PanelHeader icon="bolt" title="Since your last visit" meta={`(${lastVisitLabel})`} />
        <div className="text-[13px] py-2" style={{ color: "var(--on-surface-variant)" }}>
          Nothing new since your last visit.
        </div>
      </Panel>
    );
  }

  return (
    <Panel focused={focused}>
      <div className="flex items-baseline justify-between mb-3">
        <div className="flex items-center gap-2">
          <Icon name="bolt" size={14} color="var(--on-surface-variant)" />
          <h2 className="font-headline text-[12px] font-semibold tracking-widest uppercase"
              style={{ color: "var(--on-surface)" }}>
            Since your last visit
          </h2>
          <span className="text-[11px]" style={{ color: "var(--on-surface-variant)" }}>
            ({lastVisitLabel})
          </span>
        </div>
        <button onClick={onMarkReviewed}
                className="text-[11px] flex items-center gap-1 focus-ring"
                style={{ color: "var(--primary)" }}>
          review all <span className="kbd" style={{ marginLeft: 4 }}>R</span>
        </button>
      </div>
      <div className="space-y-0">
        {cases.map((c, i) => {
          const isFocused = focused && i === focusedIdx;
          return (
            <div key={c.caseId}
                 className="flex items-center gap-3 py-2 px-2 -mx-2 rounded row-hover cursor-pointer"
                 style={{
                   background: isFocused ? "var(--surface-low)" : "transparent",
                   outline: isFocused ? "1px solid var(--outline-variant)" : "none",
                 }}>
              <CaseChip id={c.caseId} />
              <div className="flex-1 min-w-0 flex items-center gap-3">
                <span className="text-[12px] font-mono"
                      style={{ color: "var(--primary)" }}>
                  +{c.newDocs} doc{c.newDocs === 1 ? "" : "s"}
                </span>
                {c.newActions > 0 && (
                  <span className="text-[12px]" style={{ color: "var(--on-surface-variant)" }}>
                    · {c.newActions} new action
                  </span>
                )}
                <SignificancePill tier={c.maxSignificance} />
                <span className="text-[12px] truncate" style={{ color: "var(--on-surface-variant)" }}>
                  {c.newDocs === 1 ? c.docTitles[0] : c.docTitles.slice(0, 2).join(" · ")}
                  {c.newDocs > 2 ? ` · +${c.newDocs - 2}` : ""}
                </span>
              </div>
              <button className="flex items-center gap-1 text-[11px] focus-ring px-2 py-1 rounded"
                      style={{ color: "var(--primary)" }}>
                open case <Icon name="arrow-right" size={11} color="var(--primary)"/>
              </button>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

function SignificancePill({ tier }) {
  const color =
    tier === "critical" ? "var(--critical)" :
    tier === "significant" ? "var(--amber)" :
    tier === "informational" ? "var(--on-surface-variant)" :
    "var(--outline)";
  return (
    <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded"
          style={{
            color,
            border: `1px solid ${color}`,
            fontWeight: 600,
            letterSpacing: "0.08em",
          }}>
      {tier}
    </span>
  );
}

// ——— 4. SIGNALS ———

function SignalsPanel({ signals, focused, focusedIdx }) {
  if (signals.length === 0) {
    return (
      <Panel focused={focused}>
        <PanelHeader icon="hourglass" title="Signals" />
        <div className="text-[13px] py-2" style={{ color: "var(--on-surface-variant)" }}>
          All systems quiet.
        </div>
      </Panel>
    );
  }

  return (
    <Panel focused={focused}>
      <PanelHeader icon="hourglass" title="Signals" />
      <div className="space-y-0">
        {signals.map((s, i) => {
          const isFocused = focused && i === focusedIdx;
          const iconColor = s.severity === "warn" ? "var(--amber)" : "var(--on-surface-variant)";
          const icon = s.severity === "warn" ? "bolt" : "info";
          return (
            <div key={s.id}
                 className="flex items-center gap-3 py-2 px-2 -mx-2 rounded row-hover cursor-pointer"
                 style={{
                   background: isFocused ? "var(--surface-low)" : "transparent",
                   outline: isFocused ? "1px solid var(--outline-variant)" : "none",
                 }}>
              <div className="w-5 flex justify-center">
                <Icon name={icon} size={13} color={iconColor} />
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-[13px] truncate" style={{ color: "var(--on-surface)" }}>
                  {s.title}
                </div>
                <div className="text-[11px] truncate" style={{ color: "var(--on-surface-variant)" }}>
                  {s.detail}
                </div>
              </div>
              <button className="flex items-center gap-1 text-[11px] focus-ring px-2 py-1 rounded"
                      style={{
                        color: s.severity === "warn" ? "var(--on-primary)" : "var(--primary)",
                        background: s.severity === "warn" ? "var(--primary)" : "transparent",
                        border: s.severity === "warn" ? "none" : "1px solid var(--outline-variant)",
                      }}>
                {s.action} <Icon name="arrow-right" size={11}
                                 color={s.severity === "warn" ? "var(--on-primary)" : "var(--primary)"}/>
              </button>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

// ——— 5. ACTIVE CASES grid ———

function ActiveCaseCard({ c, focused }) {
  const tierDot =
    c.tier === "delta" ? "var(--originator-own)" :
    c.tier === "imminent" ? "var(--amber)" :
    c.tier === "dormant" ? "var(--critical)" :
    "var(--outline)";

  return (
    <div className="rounded-lg p-4 cursor-pointer row-hover"
         style={{
           background: "var(--surface-lowest)",
           border: `1px solid ${focused ? "var(--primary)" : "var(--outline-variant)"}`,
           boxShadow: focused ? "0 0 0 3px rgba(87,241,219,0.06)" : "none",
           transition: "border-color .15s, box-shadow .15s",
         }}>
      {/* header */}
      <div className="flex items-start justify-between mb-2">
        <div className="font-mono text-[11px] font-semibold"
             style={{ color: "var(--on-surface)" }}>
          {c.id}
        </div>
        {c.newDocs > 0 && (
          <span className="text-[10px] font-mono font-semibold px-1.5 py-0.5 rounded"
                style={{
                  background: "var(--primary-container)",
                  color: "var(--primary)",
                }}>
            +{c.newDocs} new
          </span>
        )}
      </div>
      {/* title */}
      <div className="font-headline text-[15px] font-semibold leading-tight mb-1.5 truncate"
           style={{ color: "var(--on-surface)" }}>
        {c.title}
      </div>
      {/* proceeding + dot */}
      <div className="flex items-center gap-1.5 text-[11px] mb-3"
           style={{ color: "var(--on-surface-variant)" }}>
        <span style={{
          display: "inline-block", width: 6, height: 6, borderRadius: 999,
          background: tierDot,
        }} />
        <span>Active</span>
        <span>·</span>
        <span>{c.proceeding}</span>
      </div>
      {/* status line */}
      <div className="text-[12px] mb-2.5 line-clamp-2"
           style={{ color: "var(--on-surface-variant)", minHeight: 32 }}>
        {c.statusLine}
      </div>
      {/* next action */}
      {c.nextAction && (
        <div className="flex items-center gap-1.5 text-[12px] mb-2">
          <span style={{
            color: c.nextAction.daysUntil <= 7 ? "var(--critical)" : "var(--near)",
          }}>⚑</span>
          <span style={{
            color: c.nextAction.daysUntil <= 7 ? "var(--critical)" : "var(--on-surface)",
          }}>
            {c.nextAction.label}
          </span>
        </div>
      )}
      {/* divider */}
      <div className="divider my-3" />
      {/* metrics row */}
      <div className="flex items-center justify-between text-[11px] font-mono"
           style={{ color: "var(--on-surface-variant)" }}>
        <span className="flex items-center gap-1">
          <Icon name="euro" size={10} color="var(--on-surface-variant)" />
          {c.exposure}
        </span>
        <span>
          {c.newDocs > 0 ? `${c.newDocs} new` : "no delta"}
        </span>
        <span>
          {c.daysSinceActivity >= 90
            ? <span style={{ color: "var(--critical)" }}>{c.daysSinceActivity}d quiet</span>
            : `${c.daysSinceActivity}d`}
        </span>
      </div>
    </div>
  );
}

function ActiveCasesSection({ cases, focused, focusedIdx }) {
  if (cases.length === 0) {
    return (
      <div>
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="font-headline text-[12px] font-semibold tracking-widest uppercase"
              style={{ color: "var(--on-surface)" }}>
            Active Cases
          </h2>
        </div>
        <Panel>
          <div className="text-[13px] py-2 flex items-center gap-3" style={{ color: "var(--on-surface-variant)" }}>
            No active cases yet.
            <button className="flex items-center gap-1 text-[11px] focus-ring px-2 py-1 rounded"
                    style={{
                      color: "var(--on-primary)", background: "var(--primary)",
                    }}>
              <Icon name="add" size={11} color="var(--on-primary)"/> add new case
            </button>
          </div>
        </Panel>
      </div>
    );
  }
  return (
    <div>
      <div className="flex items-baseline justify-between mb-3 px-1">
        <h2 className="font-headline text-[12px] font-semibold tracking-widest uppercase"
            style={{ color: "var(--on-surface)" }}>
          Active Cases <span className="text-[11px] font-normal normal-case tracking-normal"
                              style={{ color: "var(--on-surface-variant)" }}>
            · {cases.length}
          </span>
        </h2>
        <button className="text-[11px] flex items-center gap-1 focus-ring"
                style={{ color: "var(--primary)" }}>
          see all <Icon name="arrow-right" size={11} color="var(--primary)"/>
        </button>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {cases.map((c, i) => (
          <ActiveCaseCard key={c.id} c={c} focused={focused && i === focusedIdx} />
        ))}
      </div>
    </div>
  );
}

// ——— 6. COMPOSITE "caught up" ———

function CaughtUp() {
  return (
    <div className="rounded-lg p-10 text-center"
         style={{
           background: "var(--surface-lowest)",
           border: "1px solid var(--outline-variant)",
         }}>
      <div className="inline-flex items-center justify-center w-12 h-12 rounded-full mb-4"
           style={{ background: "var(--primary-container)", color: "var(--primary)" }}>
        <Icon name="check" size={22} color="var(--primary)" />
      </div>
      <h2 className="font-headline text-[22px] font-semibold mb-3"
          style={{ color: "var(--on-surface)" }}>
        You're caught up.
      </h2>
      <div className="text-[13px] space-y-1 mb-5" style={{ color: "var(--on-surface-variant)" }}>
        <div>No deadlines this month.</div>
        <div>No bundles pending triage.</div>
        <div>Nothing new since your last visit.</div>
        <div>All signals quiet.</div>
      </div>
      <div className="text-[12px] italic" style={{ color: "var(--on-surface-variant)" }}>
        Your active cases are steady. Go get coffee.
      </div>
    </div>
  );
}

Object.assign(window, {
  TodayPanel, TriagePanel, DeltaPanel, SignalsPanel, ActiveCasesSection,
  ActiveCaseCard, CaughtUp, PanelHeader, CaseChip, Panel,
});

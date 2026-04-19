// Main app — composes everything

const { useState, useEffect, useMemo } = React;

function App() {
  const [tweaks, setTweaksRaw] = useState(window.__TWEAKS__);
  const [selectedDocId, setSelectedDocId] = useState(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [tweaksOpen, setTweaksOpen] = useState(false);
  const [deltaDismissed, setDeltaDismissed] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [actionFilter, setActionFilter] = useState("open");
  const [partyFilter, setPartyFilter] = useState(null);
  const [proceedingId, setProceedingId] = useState(window.CASE.activeProceedingId);
  const [procOpen, setProcOpen] = useState(false);

  const setTweaks = (t) => {
    setTweaksRaw(t);
    try { window.parent.postMessage({ type: "__edit_mode_set_keys", edits: t }, "*"); } catch(e) {}
  };

  // Theme
  useEffect(() => {
    document.documentElement.classList.toggle("dark", tweaks.theme === "dark");
    document.body.className = `density-${tweaks.density}`;
  }, [tweaks.theme, tweaks.density]);

  // Edit-mode hook
  useEffect(() => {
    const h = (e) => {
      if (e.data?.type === "__activate_edit_mode") setTweaksOpen(true);
      if (e.data?.type === "__deactivate_edit_mode") setTweaksOpen(false);
    };
    window.addEventListener("message", h);
    try { window.parent.postMessage({ type: "__edit_mode_available" }, "*"); } catch(e) {}
    return () => window.removeEventListener("message", h);
  }, []);

  // Keyboard
  useEffect(() => {
    const h = (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      if (e.key === "Escape") { setSelectedDocId(null); setChatOpen(false); setReviewOpen(false); setProcOpen(false); return; }
      if (e.key === "/" ) { e.preventDefault(); setChatOpen(true); return; }
      if (e.key === "g") setTweaks({ ...tweaks, activeView: "graph" });
      if (e.key === "t") setTweaks({ ...tweaks, activeView: "truth" });
      if (e.key === "l") setTweaks({ ...tweaks, activeView: "timeline" });
      if (e.key === "$") setTweaks({ ...tweaks, activeView: "fin" });
      if (e.key === "?") alert("Shortcuts:\n/ Ask AI   g/t/l/$ views   Cmd+K search   Esc close");
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [tweaks]);

  const proceeding = window.PROCEEDINGS.find(p => p.id === proceedingId);

  const navDoc = (dir) => {
    const list = window.DOCUMENTS.filter(d => d.id !== "bundle");
    const idx = list.findIndex(d => d.id === selectedDocId);
    const next = list[(idx + dir + list.length) % list.length];
    setSelectedDocId(next.id);
  };

  const highlightIds = window.DELTA.new_doc_ids;

  return (
    <div className="flex" style={{ height: "100vh", background: "var(--surface)" }}>
      <Rail active="cases" triageCount={window.TRIAGE_BUNDLES?.length || 2}/>
      <div className="flex flex-col flex-1 min-w-0">
      {/* Top bar */}
      <TopBar proceeding={proceeding} procOpen={procOpen} setProcOpen={setProcOpen}
              setProceedingId={setProceedingId}
              activeView={tweaks.activeView} setActiveView={v => setTweaks({ ...tweaks, activeView: v })}
              onChat={() => setChatOpen(true)}/>

      {/* Secondary header: breadcrumbs + filters */}
      <SecondaryHeader />

      {/* Delta banner */}
      {!deltaDismissed && (
        <DeltaBanner delta={window.DELTA}
                     onReview={() => setReviewOpen(true)}
                     onDismiss={() => setDeltaDismissed(true)}/>
      )}

      {/* Body grid */}
      <div className="grid"
           style={{
             flex: "1 1 0",
             minHeight: 0,
             gridTemplateColumns: "280px 1fr",
             gridTemplateRows: "minmax(320px,1fr) minmax(200px,240px)",
           }}>
        {/* Left column */}
        <aside className="custom-scrollbar border-r flex flex-col"
               style={{ gridRow: "1 / span 2", overflowY: "auto", minHeight: 0,
                        borderColor: "var(--outline-variant)", background: "var(--surface-low)" }}>
          <AIBriefPanel brief={window.AI_BRIEF} updatedAt={window.CASE.aiBriefUpdatedAt} onRefresh={() => {}}/>
          <div className="divider mx-4"/>
          <PartiesPanel parties={window.PARTIES} onPartyFilter={(c) => setPartyFilter(partyFilter === c ? null : c)} activeFilter={partyFilter}/>
          <div className="divider mx-4"/>
          <FinancialsPanel financials={window.FINANCIALS} onBreakdown={() => setTweaks({ ...tweaks, activeView: "fin" })}/>
          <div className="divider mx-4"/>
          <div className="p-4 mt-auto text-[10.5px] font-mono" style={{ color: "var(--on-surface-variant)" }}>
            <div className="flex items-center gap-2 mb-1">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: "var(--originator-own)" }}/>
              local · ollama qwen3.5:9b
            </div>
            <div>nothing leaves the machine</div>
          </div>
        </aside>

        {/* Main canvas */}
        <main style={{ overflow: "hidden", minHeight: 0, background: "var(--surface)" }}>
          {tweaks.activeView === "graph" && (
            <CorrespondenceGraph filter={tweaks.graphFilter} onSelect={setSelectedDocId}
                                 selectedId={selectedDocId} highlightIds={highlightIds}/>
          )}
          {tweaks.activeView === "timeline" && <TimelineView onSelect={setSelectedDocId}/>}
          {tweaks.activeView === "truth" && <TruthMap/>}
          {tweaks.activeView === "fin" && <FinancialsView/>}
        </main>

        {/* Action Items */}
        <section className="border-t" style={{ minHeight: 0, borderColor: "var(--outline-variant)" }}>
          <ActionItemsPanel items={window.ACTION_ITEMS} filter={actionFilter} setFilter={setActionFilter} onOpenDoc={setSelectedDocId}/>
        </section>
      </div>

      {/* Floating Ask AI button */}
      {!chatOpen && (
        <button onClick={() => setChatOpen(true)}
                className="fixed right-5 bottom-5 z-30 flex items-center gap-2 px-3.5 py-2.5 rounded-full"
                style={{ background: "var(--primary)", color: "var(--on-primary)", boxShadow: "0 8px 24px rgba(0,0,0,0.35)" }}>
          <Icon name="sparkle" size={16}/>
          <span className="text-[12.5px] font-semibold">Ask AI</span>
          <span className="kbd ml-1" style={{ background: "rgba(0,0,0,0.15)", borderColor: "rgba(0,0,0,0.2)", color: "var(--on-primary)" }}>/</span>
        </button>
      )}

      {/* Overlays */}
      {selectedDocId && <DocumentHUD docId={selectedDocId} onClose={() => setSelectedDocId(null)} onNav={navDoc}/>}
      {chatOpen && <CaseChat onClose={() => setChatOpen(false)}/>}
      {reviewOpen && <ReviewModal onClose={() => setReviewOpen(false)} onOpenDoc={setSelectedDocId}/>}
      <TweaksPanel open={tweaksOpen} tweaks={tweaks} setTweaks={setTweaks}/>

      {/* Proceeding dropdown */}
      {procOpen && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setProcOpen(false)}/>
          <div className="fixed top-14 left-[160px] z-50 w-[320px] rounded-md fade-in"
               style={{ background: "var(--surface-container)", border: "1px solid var(--outline-variant)", boxShadow: "0 18px 40px rgba(0,0,0,0.35)" }}>
            <div className="px-3 py-2 text-[10.5px] uppercase tracking-[0.12em]"
                 style={{ color: "var(--on-surface-variant)" }}>Switch proceeding</div>
            {window.PROCEEDINGS.map((p, i) => (
              <button key={p.id}
                      onClick={() => { setProceedingId(p.id); setProcOpen(false); }}
                      className="w-full text-left px-3 py-2 row-hover flex items-center gap-3 border-t"
                      style={{ borderColor: "var(--outline-variant)" }}>
                <span className="kbd">{i+1}</span>
                <div className="flex-1">
                  <div className="text-[12.5px] font-semibold" style={{ color: "var(--on-surface)" }}>{p.court}</div>
                  <div className="text-[11px] font-mono" style={{ color: "var(--on-surface-variant)" }}>{p.az} · {p.level}</div>
                </div>
                <span className="chip">{p.docs} docs</span>
                {p.id === proceedingId && <Icon name="check" size={16} color="var(--primary)"/>}
              </button>
            ))}
          </div>
        </>
      )}
      </div>
    </div>
  );
}

/* --------------- Top bar --------------- */
function TopBar({ proceeding, procOpen, setProcOpen, activeView, setActiveView, onChat }) {
  return (
    <header className="flex items-center px-4 h-14 flex-shrink-0 border-b"
            style={{ gap: 12, borderColor: "var(--outline-variant)", background: "var(--surface)", minWidth: 0 }}>
      {/* Case identity */}
      <div className="flex items-center gap-2.5 min-w-0 flex-shrink">
        <span className="font-mono text-[11.5px] px-1.5 py-0.5 rounded flex-shrink-0"
              style={{ background: "var(--surface-high)", color: "var(--on-surface-variant)" }}>
          {window.CASE.id}
        </span>
        <span className="font-headline text-[14px] font-semibold truncate"
              style={{ color: "var(--on-surface)", minWidth: 0 }}>
          {window.CASE.title}
        </span>
        <span className="flex items-center gap-1.5 text-[11.5px] flex-shrink-0"
              style={{ color: "var(--on-surface-variant)" }}>
          <span className="inline-block h-2 w-2 rounded-full" style={{ background: "var(--originator-own)" }}/>
          Active
        </span>
        <span className="text-[11.5px] flex-shrink-0" style={{ color: "var(--on-surface-variant)" }}>· {window.CASE.tempoDays}d</span>
      </div>

      {/* Proceeding switcher */}
      <button onClick={() => setProcOpen(!procOpen)}
              className="flex items-center gap-2 px-2.5 h-8 rounded-md text-[12px] flex-shrink-0"
              style={{ background: "var(--surface-container)", border: "1px solid var(--outline-variant)" }}>
        <Icon name="gavel" size={14} color="var(--primary)"/>
        <span className="font-semibold">{proceeding.court}</span>
        <span className="font-mono text-[10.5px] hidden lg:inline" style={{ color: "var(--on-surface-variant)" }}>{proceeding.az}</span>
        <Icon name="chevron-down" size={16} color="var(--on-surface-variant)"/>
      </button>

      {/* View tabs */}
      <div className="flex rounded-md overflow-hidden flex-shrink-0" style={{ background: "var(--surface-container)", border: "1px solid var(--outline-variant)" }}>
        {[
          { k: "graph",    l: "Graph",     ic: "hub"       },
          { k: "truth",    l: "Truth",     ic: "scale"     },
          { k: "timeline", l: "Timeline",  ic: "timeline"  },
          { k: "fin",      l: "€",         ic: "euro"      },
        ].map(t => {
          const on = activeView === t.k;
          return (
            <button key={t.k} onClick={() => setActiveView(t.k)}
                    className="flex items-center gap-1.5 px-2.5 h-8 text-[11.5px]"
                    style={on
                      ? { background: "var(--primary-container)", color: "var(--primary)", fontWeight: 600 }
                      : { color: "var(--on-surface-variant)" }}>
              <Icon name={t.ic === "euro" ? "euro" : t.ic} size={14} />
              <span className="hidden xl:inline">{t.l}</span>
            </button>
          );
        })}
      </div>

      <div className="ml-auto flex items-center gap-1.5 flex-shrink-0">
        <button onClick={onChat}
                className="h-8 px-2.5 rounded-md flex items-center gap-1.5 text-[12px]"
                style={{ background: "var(--surface-container)", border: "1px solid var(--primary)", color: "var(--primary)" }}
                title="Ask AI (/)">
          <Icon name="sparkle" size={14}/>
          <span className="hidden md:inline font-medium">Ask AI</span>
          <span className="kbd" style={{ marginLeft: 2 }}>/</span>
        </button>
      </div>
    </header>
  );
}

/* --------------- Secondary header --------------- */
function SecondaryHeader() {
  return (
    <div className="flex items-center gap-3 px-5 h-9 text-[11.5px] border-b"
         style={{ borderColor: "var(--outline-variant)", background: "var(--surface-low)" }}>
      <span style={{ color: "var(--on-surface-variant)" }}>Cases</span>
      <span style={{ color: "var(--outline)" }}>›</span>
      <span style={{ color: "var(--on-surface-variant)" }}>Family</span>
      <span style={{ color: "var(--outline)" }}>›</span>
      <span style={{ color: "var(--on-surface)" }}>{window.CASE.title}</span>

      <div className="ml-auto flex items-center gap-4 text-[11px]" style={{ color: "var(--on-surface-variant)" }}>
        <span className="flex items-center gap-1.5">
          <Icon name="filter" size={13}/>
          significance: <b style={{ color: "var(--primary)" }}>significant+</b>
        </span>
        <span>·</span>
        <span>scope: <b style={{ color: "var(--on-surface)" }}>current proceeding</b></span>
        <span>·</span>
        <span className="font-mono">41 nodes · 38 edges</span>
      </div>
    </div>
  );
}

/* --------------- Alternate views (lightweight) --------------- */
function TimelineView({ onSelect }) {
  const docs = [...window.DOCUMENTS].filter(d => d.id !== "bundle").sort((a,b) => (a.date < b.date ? -1 : 1));
  return (
    <div className="h-full overflow-auto custom-scrollbar p-6">
      <div className="max-w-[780px] mx-auto">
        <h2 className="font-headline text-[14px] font-bold tracking-[0.08em] mb-4"
            style={{ color: "var(--on-surface-variant)" }}>TIMELINE · AG HAMBURG</h2>
        <ol className="relative border-l pl-6 space-y-3"
            style={{ borderColor: "var(--outline-variant)" }}>
          {docs.map(d => (
            <li key={d.id} className="relative">
              <span className="absolute -left-[30px] top-2 h-2.5 w-2.5 rounded-full"
                    style={{ background: `var(--originator-${d.lane === "own" ? "own" : d.lane})` }}/>
              <button onClick={() => onSelect(d.id)}
                      className="w-full text-left p-3 rounded-md row-hover flex items-center gap-3"
                      style={{ background: "var(--surface-container)", border: "1px solid var(--outline-variant)" }}>
                <span className="font-mono text-[11px] w-20" style={{ color: "var(--on-surface-variant)" }}>{d.date}</span>
                <span className="font-headline text-[13px] font-semibold flex-1">{d.title}</span>
                <span className="chip">{d.sig}</span>
              </button>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

function TruthMap() {
  const claims = [
    { id: "c1", text: "Opposing party failed to pay maintenance Jan–Feb 2026", status: "established", evidence: 3 },
    { id: "c2", text: "Child's welfare better served in current custody arrangement", status: "contested", evidence: 5 },
    { id: "c3", text: "Opposing side concealed income from secondary employment", status: "asserted", evidence: 2 },
    { id: "c4", text: "Third-party recommendation cites outdated 2024 assessment", status: "refuted", evidence: 1 },
  ];
  const color = s => ({
    established: "var(--originator-own)",
    contested:   "var(--amber)",
    asserted:    "var(--originator-unknown)",
    refuted:     "var(--originator-opposing)",
  }[s]);
  return (
    <div className="h-full overflow-auto custom-scrollbar p-6">
      <h2 className="font-headline text-[14px] font-bold tracking-[0.08em] mb-1"
          style={{ color: "var(--on-surface-variant)" }}>TRUTH MAP</h2>
      <div className="text-[11.5px] mb-4" style={{ color: "var(--on-surface-variant)" }}>
        Contested factual & legal assertions. Evidence chain per claim — click to expand.
      </div>
      <div className="grid grid-cols-2 gap-3">
        {claims.map(c => (
          <div key={c.id} className="p-4 rounded-md"
               style={{ background: "var(--surface-container)", border: "1px solid var(--outline-variant)", borderLeft: `3px solid ${color(c.status)}` }}>
            <div className="flex items-center gap-2 mb-2">
              <span className="chip" style={{ color: color(c.status), borderColor: color(c.status) }}>{c.status}</span>
              <span className="text-[10.5px] font-mono ml-auto" style={{ color: "var(--on-surface-variant)" }}>{c.evidence} evidence</span>
            </div>
            <div className="text-[13px] leading-snug" style={{ color: "var(--on-surface)" }}>{c.text}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

function FinancialsView() {
  const rows = [
    { date: "2026-02-24", doc: "Kostenvorschussanf.", cat: "GKG — advance",  amount: 747, status: "paid" },
    { date: "2026-03-10", doc: "Klageerwiderung",    cat: "RVG 3100 Verfahren", amount: 546, status: "accrued" },
    { date: "2026-03-28", doc: "Antrag §91",          cat: "Opposing §91 claim", amount: 1240, status: "claimed" },
    { date: "2026-04-02", doc: "Beschluss PKH",       cat: "PKH suspension",  amount: 0, status: "n/a" },
    { date: "2026-04-02", doc: "Internal",            cat: "RVG 3104 Termin (est.)", amount: 247, status: "accrued" },
  ];
  const tot = rows.reduce((s, r) => s + r.amount, 0);
  return (
    <div className="h-full overflow-auto custom-scrollbar p-6">
      <h2 className="font-headline text-[14px] font-bold tracking-[0.08em] mb-4"
          style={{ color: "var(--on-surface-variant)" }}>FINANCIALS · RVG / GKG BREAKDOWN</h2>
      <div className="grid grid-cols-4 gap-3 mb-5">
        {[
          { l: "Total exposure", v: `${tot.toLocaleString("de-DE")} €` },
          { l: "Paid",           v: "747 €" },
          { l: "Accrued",        v: "793 €" },
          { l: "Opposing §91",   v: "1.240 €" },
        ].map(m => (
          <div key={m.l} className="p-4 rounded-md"
               style={{ background: "var(--surface-container)", border: "1px solid var(--outline-variant)" }}>
            <div className="text-[10.5px] uppercase tracking-[0.1em]" style={{ color: "var(--on-surface-variant)" }}>{m.l}</div>
            <div className="font-headline text-[20px] font-bold mt-1">{m.v}</div>
          </div>
        ))}
      </div>
      <div className="rounded-md overflow-hidden" style={{ border: "1px solid var(--outline-variant)" }}>
        <div className="grid grid-cols-[120px_1fr_1fr_120px_100px] text-[10.5px] uppercase tracking-[0.1em] px-3 py-2"
             style={{ background: "var(--surface-low)", color: "var(--on-surface-variant)" }}>
          <div>Date</div><div>Source document</div><div>Category</div><div className="text-right">Amount</div><div className="text-right">Status</div>
        </div>
        {rows.map((r, i) => (
          <div key={i} className="grid grid-cols-[120px_1fr_1fr_120px_100px] text-[12px] px-3 py-2.5 border-t items-center"
               style={{ borderColor: "var(--outline-variant)", background: "var(--surface-container)" }}>
            <div className="font-mono" style={{ color: "var(--on-surface-variant)" }}>{r.date}</div>
            <div>{r.doc}</div>
            <div style={{ color: "var(--on-surface-variant)" }}>{r.cat}</div>
            <div className="text-right font-mono">{r.amount ? r.amount.toLocaleString("de-DE") + " €" : "—"}</div>
            <div className="text-right"><span className="chip">{r.status}</span></div>
          </div>
        ))}
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App/>);

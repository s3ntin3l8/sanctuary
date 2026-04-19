// Home page — top-level app

const { useState, useEffect, useMemo } = React;

// ——— Greeting bar ———

function GreetingBar({ user, date }) {
  const h = date.getHours();
  const greeting = h < 5 ? "Still up" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  const dateStr = date.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  const weekday = date.toLocaleDateString("en-US", { weekday: "long" });
  return (
    <div className="flex items-baseline justify-between px-8 pt-8 pb-6">
      <div>
        <h1 className="font-headline text-[28px] font-semibold leading-tight"
            style={{ color: "var(--on-surface)" }}>
          {greeting}, {user.name}.
        </h1>
        <div className="text-[13px] mt-1" style={{ color: "var(--on-surface-variant)" }}>
          {weekday} · {dateStr}
        </div>
      </div>
      <div className="flex items-center gap-3">
        <button className="flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded focus-ring"
                style={{
                  border: "1px solid var(--outline-variant)",
                  color: "var(--on-surface-variant)",
                  background: "var(--surface-lowest)",
                }}>
          <Icon name="search" size={13} color="var(--on-surface-variant)"/>
          <span>Search</span>
          <span className="kbd" style={{ marginLeft: 4 }}>⌘K</span>
        </button>
        <button className="flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded focus-ring"
                style={{
                  border: "1px solid var(--outline-variant)",
                  color: "var(--on-surface-variant)",
                  background: "var(--surface-lowest)",
                }}>
          <Icon name="add" size={13} color="var(--on-surface-variant)"/>
          New case
        </button>
      </div>
    </div>
  );
}

// ——— Keyboard cheatsheet overlay ———

function CheatSheet({ onClose }) {
  const rows = [
    ["j / k", "next / previous item"],
    ["Enter", "open highlighted item"],
    ["t", "jump to Today"],
    ["i", "jump to Awaiting Triage"],
    ["d", "jump to Since your last visit"],
    ["s", "jump to Signals"],
    ["c", "jump to Active Cases"],
    ["r", "mark delta reviewed"],
    ["⌘K", "global search"],
    ["?", "this cheat sheet"],
  ];
  return (
    <div className="fixed inset-0 flex items-center justify-center z-50"
         style={{ background: "rgba(0,0,0,0.45)" }}
         onClick={onClose}>
      <div className="rounded-lg p-6 w-[420px]"
           style={{ background: "var(--surface-lowest)", border: "1px solid var(--outline-variant)" }}
           onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-headline text-[16px] font-semibold"
              style={{ color: "var(--on-surface)" }}>
            Keyboard shortcuts
          </h3>
          <button onClick={onClose} className="focus-ring p-1 rounded">
            <Icon name="close" size={14} color="var(--on-surface-variant)"/>
          </button>
        </div>
        <div className="space-y-1.5">
          {rows.map(([k, v]) => (
            <div key={k} className="flex items-center justify-between text-[12px] py-1">
              <span style={{ color: "var(--on-surface-variant)" }}>{v}</span>
              <span className="kbd">{k}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ——— Tweaks panel ———

function TweaksPanel({ tweaks, setTweaks, onClose }) {
  const set = (k, v) => setTweaks({ ...tweaks, [k]: v });
  return (
    <div className="tweaks">
      <div className="flex items-center justify-between mb-3">
        <div className="font-headline font-semibold text-[13px]" style={{ color: "var(--on-surface)" }}>
          Tweaks
        </div>
        <button onClick={onClose} className="focus-ring p-1 rounded">
          <Icon name="close" size={13} color="var(--on-surface-variant)"/>
        </button>
      </div>
      <div className="space-y-3">
        <TweakGroup label="Theme">
          {["light","dark"].map(v => (
            <TweakBtn key={v} active={tweaks.theme===v} onClick={() => set("theme", v)}>{v}</TweakBtn>
          ))}
        </TweakGroup>
        <TweakGroup label="Density">
          {["compact","default","comfortable"].map(v => (
            <TweakBtn key={v} active={tweaks.density===v} onClick={() => set("density", v)}>{v}</TweakBtn>
          ))}
        </TweakGroup>
        <TweakGroup label="State">
          {["default","caught_up","empty_triage"].map(v => (
            <TweakBtn key={v} active={tweaks.state===v} onClick={() => set("state", v)}>
              {v.replace("_", " ")}
            </TweakBtn>
          ))}
        </TweakGroup>
        <TweakGroup label="Visits ago">
          {["6h","24h","3d"].map(v => (
            <TweakBtn key={v} active={tweaks.lastVisit===v} onClick={() => set("lastVisit", v)}>{v}</TweakBtn>
          ))}
        </TweakGroup>
      </div>
      <div className="text-[10px] mt-3 pt-3"
           style={{ color: "var(--on-surface-variant)", borderTop: "1px solid var(--outline-variant)" }}>
        Press <span className="kbd">?</span> for shortcuts
      </div>
    </div>
  );
}

function TweakGroup({ label, children }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest mb-1.5"
           style={{ color: "var(--on-surface-variant)" }}>
        {label}
      </div>
      <div className="flex gap-1 flex-wrap">{children}</div>
    </div>
  );
}

function TweakBtn({ active, onClick, children }) {
  return (
    <button onClick={onClick}
            className="text-[11px] px-2 py-1 rounded focus-ring"
            style={{
              background: active ? "var(--primary)" : "var(--surface-lowest)",
              color: active ? "var(--on-primary)" : "var(--on-surface-variant)",
              border: `1px solid ${active ? "var(--primary)" : "var(--outline-variant)"}`,
              textTransform: "capitalize",
            }}>
      {children}
    </button>
  );
}

// ——— Main App ———

const PANELS = ["today", "triage", "delta", "signals", "cases"];

function HomeApp() {
  const [tweaks, setTweaksRaw] = useState(window.__TWEAKS__);
  const [focusedPanel, setFocusedPanel] = useState("today");
  const [focusedIdx, setFocusedIdx] = useState(0);
  const [tweaksOpen, setTweaksOpen] = useState(false);
  const [cheatOpen, setCheatOpen] = useState(false);
  const [deltaReviewed, setDeltaReviewed] = useState(false);

  const setTweaks = (t) => {
    setTweaksRaw(t);
    try { window.parent.postMessage({ type: "__edit_mode_set_keys", edits: t }, "*"); } catch(e) {}
  };

  // Theme
  useEffect(() => {
    document.documentElement.classList.toggle("dark", tweaks.theme === "dark");
    document.body.className = `density-${tweaks.density || "default"}`;
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

  // Compute panel data based on tweak state
  const { todayItems, triageBundles, deltaCases, signals, activeCases, caughtUp } = useMemo(() => {
    const state = tweaks.state || "default";
    if (state === "caught_up") {
      return {
        todayItems: [], triageBundles: [], deltaCases: [], signals: [],
        activeCases: window.ACTIVE_CASES.map(c => ({ ...c, newDocs: 0, tier: "normal" })),
        caughtUp: true,
      };
    }
    if (state === "empty_triage") {
      return {
        todayItems: window.TODAY_ITEMS,
        triageBundles: [],
        deltaCases: deltaReviewed ? [] : window.DELTA_CASES,
        signals: window.SIGNALS,
        activeCases: window.ACTIVE_CASES,
        caughtUp: false,
      };
    }
    return {
      todayItems: window.TODAY_ITEMS,
      triageBundles: window.TRIAGE_BUNDLES,
      deltaCases: deltaReviewed ? [] : window.DELTA_CASES,
      signals: window.SIGNALS,
      activeCases: window.ACTIVE_CASES,
      caughtUp: false,
    };
  }, [tweaks.state, deltaReviewed]);

  const lastVisitLabel = (() => {
    const v = tweaks.lastVisit || "6h";
    return v === "6h" ? "6 hours ago" : v === "24h" ? "yesterday" : "3 days ago";
  })();

  const panelItemCount = {
    today: todayItems.length,
    triage: triageBundles.length,
    delta: deltaCases.length,
    signals: signals.length,
    cases: activeCases.length,
  };

  // Keyboard
  useEffect(() => {
    const h = (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
      if (cheatOpen && e.key === "Escape") { setCheatOpen(false); return; }

      const jumps = { t: "today", i: "triage", d: "delta", s: "signals", c: "cases" };
      if (jumps[e.key]) {
        e.preventDefault();
        setFocusedPanel(jumps[e.key]);
        setFocusedIdx(0);
        return;
      }
      if (e.key === "j") {
        e.preventDefault();
        setFocusedIdx(i => Math.min(i + 1, (panelItemCount[focusedPanel] || 1) - 1));
        return;
      }
      if (e.key === "k") {
        e.preventDefault();
        setFocusedIdx(i => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "r") {
        e.preventDefault();
        setDeltaReviewed(true);
        return;
      }
      if (e.key === "?") { e.preventDefault(); setCheatOpen(true); return; }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [focusedPanel, panelItemCount, cheatOpen]);

  return (
    <div className="flex" style={{ height: "100vh", background: "var(--surface)" }}>
      <Rail active="home" triageCount={window.TRIAGE_BUNDLES?.length || 0} />
      <div className="flex-1 overflow-y-auto custom-scrollbar">
        <div style={{ maxWidth: 1180, margin: "0 auto" }}>
          <GreetingBar user={window.HOME.user} date={window.HOME.now} />

          <div className="px-8 pb-16 space-y-4">
            {caughtUp ? (
              <>
                <CaughtUp />
                <div className="pt-6">
                  <ActiveCasesSection cases={activeCases}
                                      focused={focusedPanel === "cases"}
                                      focusedIdx={focusedIdx} />
                </div>
              </>
            ) : (
              <>
                <TodayPanel items={todayItems}
                            focused={focusedPanel === "today"}
                            focusedIdx={focusedIdx} />
                <TriagePanel bundles={triageBundles}
                             focused={focusedPanel === "triage"}
                             focusedIdx={focusedIdx} />
                <DeltaPanel cases={deltaCases}
                            lastVisitLabel={lastVisitLabel}
                            focused={focusedPanel === "delta"}
                            focusedIdx={focusedIdx}
                            onMarkReviewed={() => setDeltaReviewed(true)} />
                <SignalsPanel signals={signals}
                              focused={focusedPanel === "signals"}
                              focusedIdx={focusedIdx} />
                <div className="pt-4">
                  <ActiveCasesSection cases={activeCases}
                                      focused={focusedPanel === "cases"}
                                      focusedIdx={focusedIdx} />
                </div>
              </>
            )}

            {/* footer hint */}
            <div className="flex items-center justify-center gap-2 pt-8 text-[11px]"
                 style={{ color: "var(--on-surface-variant)" }}>
              <span className="kbd">j</span><span className="kbd">k</span>
              <span>navigate</span>
              <span>·</span>
              <span className="kbd">t</span><span className="kbd">i</span><span className="kbd">d</span><span className="kbd">s</span><span className="kbd">c</span>
              <span>jump panels</span>
              <span>·</span>
              <span className="kbd">r</span>
              <span>mark reviewed</span>
              <span>·</span>
              <span className="kbd">?</span>
              <span>all shortcuts</span>
            </div>
          </div>
        </div>
      </div>

      {tweaksOpen && <TweaksPanel tweaks={tweaks} setTweaks={setTweaks} onClose={() => setTweaksOpen(false)} />}
      {cheatOpen && <CheatSheet onClose={() => setCheatOpen(false)} />}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<HomeApp />);

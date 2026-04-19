// Left rail — thin 56px icon nav shared across pages

function Rail({ active, triageCount = 2, notifCount = 0 }) {
  const [hover, setHover] = React.useState(null);
  const [paletteOpen, setPaletteOpen] = React.useState(false);
  const [isDark, setIsDark] = React.useState(() =>
    typeof document !== "undefined" && document.documentElement.classList.contains("dark"));

  React.useEffect(() => {
    const h = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen(true);
      } else if (e.key === "Escape") {
        setPaletteOpen(false);
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  // Keep local state in sync with the <html> class (tweaks panel can flip it)
  React.useEffect(() => {
    const obs = new MutationObserver(() => {
      setIsDark(document.documentElement.classList.contains("dark"));
    });
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);

  const toggleTheme = () => {
    const nowDark = !document.documentElement.classList.contains("dark");
    document.documentElement.classList.toggle("dark", nowDark);
    setIsDark(nowDark);
    try {
      window.parent.postMessage({
        type: "__edit_mode_set_keys",
        edits: { theme: nowDark ? "dark" : "light" },
      }, "*");
    } catch (e) {}
  };

  const topItems = [
    { k: "home",    icon: "home",   label: "Home",    href: "Home.html" },
    { k: "triage",  icon: "triage", label: "Triage",  href: "#",           badge: triageCount },
    { k: "cases",   icon: "folder", label: "Cases",   href: "Case Dashboard.html" },
  ];
  const bottomItems = [
    { k: "cmd",      icon: "command",  label: "Command (⌘K)", onClick: () => setPaletteOpen(true), kbd: "⌘K" },
    { k: "upload",   icon: "upload",   label: "Upload" },
    { k: "notif",    icon: "bell",     label: "Notifications", badge: notifCount || null },
    { k: "theme",    icon: "contrast", label: isDark ? "Light mode" : "Dark mode", onClick: toggleTheme },
    { k: "settings", icon: "settings", label: "Settings" },
    { k: "user",     icon: "user",     label: "Björn" },
  ];

  const RailBtn = ({ it }) => {
    const on = active === it.k;
    const isHover = hover === it.k;
    const Cmp = it.href ? "a" : "button";
    return (
      <Cmp href={it.href}
           onClick={it.onClick}
           onMouseEnter={() => setHover(it.k)}
           onMouseLeave={() => setHover(null)}
           className="relative flex items-center justify-center focus-ring"
           style={{
             width: 40, height: 40, borderRadius: 8,
             background: on ? "var(--primary-container)" : (isHover ? "var(--surface-high)" : "transparent"),
             color: on ? "var(--primary)" : "var(--on-surface-variant)",
             transition: "background .12s",
             textDecoration: "none",
           }}>
        <Icon name={it.icon} size={18} color={on ? "var(--primary)" : "var(--on-surface-variant)"} />
        {it.badge > 0 && (
          <span className="absolute font-mono font-semibold"
                style={{
                  top: 2, right: 2, minWidth: 15, height: 15, padding: "0 4px",
                  borderRadius: 999, background: "var(--amber)", color: "#0b1326",
                  fontSize: 9.5, lineHeight: "15px", textAlign: "center",
                }}>
            {it.badge}
          </span>
        )}
        {on && (
          <span style={{
            position: "absolute", left: -12, top: 8, bottom: 8, width: 3,
            background: "var(--primary)", borderRadius: "0 2px 2px 0",
          }}/>
        )}
        {/* Hover label */}
        {isHover && (
          <span className="absolute font-medium"
                style={{
                  left: 48, top: "50%", transform: "translateY(-50%)",
                  padding: "5px 9px", borderRadius: 6,
                  background: "var(--surface-highest)",
                  color: "var(--on-surface)",
                  border: "1px solid var(--outline-variant)",
                  fontSize: 11, whiteSpace: "nowrap", zIndex: 100,
                  boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
                  pointerEvents: "none",
                }}>
            {it.label}
            {it.kbd && <span className="kbd" style={{ marginLeft: 6 }}>{it.kbd}</span>}
          </span>
        )}
      </Cmp>
    );
  };

  return (
    <>
      <nav className="flex flex-col items-center flex-shrink-0"
           style={{
             width: 56, height: "100vh",
             background: "var(--surface-lowest)",
             borderRight: "1px solid var(--outline-variant)",
             paddingTop: 10, paddingBottom: 10,
             position: "relative", zIndex: 40,
           }}>
        {/* Brand */}
        <a href="Home.html" className="flex items-center justify-center focus-ring"
           style={{ width: 40, height: 40, borderRadius: 8, marginBottom: 6 }}>
          <svg width="22" height="22" viewBox="0 0 24 24">
            <path d="M12 2 L22 7 V13 C22 18 17 22 12 22 C7 22 2 18 2 13 V7 Z"
                  fill="none" stroke="var(--primary)" strokeWidth="1.6"/>
            <circle cx="12" cy="12" r="2.5" fill="var(--primary)"/>
          </svg>
        </a>

        <div className="divider" style={{ width: 28, margin: "4px 0 8px" }}/>

        {/* Primary nav */}
        <div className="flex flex-col gap-1">
          {topItems.map(it => <RailBtn key={it.k} it={it}/>)}
        </div>

        <div style={{ flex: 1 }}/>

        {/* Utility cluster */}
        <div className="flex flex-col gap-1 items-center">
          {bottomItems.map(it => <RailBtn key={it.k} it={it}/>)}
        </div>
      </nav>

      {paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)}/>}
    </>
  );
}

function CommandPalette({ onClose }) {
  const [q, setQ] = React.useState("");
  const navigate = [
    { label: "home",               href: "Home.html" },
    { label: "triage",             href: "#" },
    { label: "case ADV-024-A",     href: "Case Dashboard.html" },
    { label: "case ADV-031-B — Vane vs. Vane", href: "#" },
    { label: "document #47",       href: "#" },
  ];
  const search = [
    { label: '"Müller"',   meta: "3 cases · 12 documents · 2 contacts" },
    { label: '"Frist"',    meta: "5 action items · 8 documents" },
    { label: '"Kostenvorschuss"', meta: "2 documents" },
  ];
  const actions = [
    { label: "upload document" },
    { label: "open Gmail settings" },
    { label: "add new case" },
    { label: "ask AI (global)" },
  ];

  const filter = (arr) => q.trim() === "" ? arr :
    arr.filter(x => (x.label + " " + (x.meta||"")).toLowerCase().includes(q.toLowerCase()));

  const Section = ({ title, items, accent }) => items.length === 0 ? null : (
    <div className="mb-2">
      <div className="font-headline text-[10px] font-semibold tracking-widest uppercase px-3 py-1.5"
           style={{ color: "var(--on-surface-variant)" }}>
        {title}
      </div>
      {items.map((it, i) => (
        <a key={i} href={it.href || "#"}
           onClick={onClose}
           className="flex items-center gap-3 px-3 py-2 row-hover cursor-pointer"
           style={{ textDecoration: "none", color: "var(--on-surface)" }}>
          <span style={{ color: accent || "var(--on-surface-variant)", fontFamily: "JetBrains Mono, monospace", fontSize: 11 }}>&gt;</span>
          <span className="text-[13px] flex-1">{it.label}</span>
          {it.meta && <span className="text-[11px]" style={{ color: "var(--on-surface-variant)" }}>{it.meta}</span>}
        </a>
      ))}
    </div>
  );

  return (
    <div onClick={onClose}
         style={{
           position: "fixed", inset: 0, background: "rgba(0,0,0,0.55)",
           zIndex: 100, display: "flex", alignItems: "flex-start", justifyContent: "center",
           paddingTop: 120,
         }}>
      <div onClick={e => e.stopPropagation()}
           style={{
             width: 560, maxWidth: "90vw",
             background: "var(--surface-lowest)",
             border: "1px solid var(--outline-variant)",
             borderRadius: 12,
             boxShadow: "0 20px 60px rgba(0,0,0,0.5)",
             overflow: "hidden",
           }}>
        <div className="flex items-center gap-3 px-4 py-3"
             style={{ borderBottom: "1px solid var(--outline-variant)" }}>
          <Icon name="command" size={16} color="var(--primary)"/>
          <input autoFocus value={q} onChange={e => setQ(e.target.value)}
                 placeholder="Type a command, search, or navigate…"
                 className="flex-1 bg-transparent outline-none text-[14px]"
                 style={{ color: "var(--on-surface)" }}/>
          <span className="kbd">ESC</span>
        </div>
        <div className="py-2 max-h-[60vh] overflow-auto custom-scrollbar">
          <Section title="Navigate" items={filter(navigate)} accent="var(--primary)"/>
          <Section title="Search"   items={filter(search)}   accent="var(--originator-own)"/>
          <Section title="Actions"  items={filter(actions)}  accent="var(--amber)"/>
        </div>
      </div>
    </div>
  );
}

window.Rail = Rail;
window.CommandPalette = CommandPalette;

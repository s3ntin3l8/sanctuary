// Overlays: Document HUD slide-in, Case AI Chat, Delta banner modal, Tweaks panel

function DeltaBanner({ delta, onReview, onDismiss }) {
  return (
    <div className="px-5 py-2.5 flex items-center gap-3 text-[12.5px] fade-in"
         style={{
           background: "linear-gradient(90deg, rgba(251,191,36,0.12), rgba(251,191,36,0.03))",
           borderBottom: "1px solid var(--outline-variant)",
           color: "var(--on-surface)",
         }}>
      <Icon name="bolt" size={16} color="var(--amber)"/>
      <span>
        <b>{delta.new_doc_ids.length} new documents</b> since your last visit
        <span style={{ color: "var(--on-surface-variant)" }}> — {delta.new_actions} action added</span>
      </span>
      <div className="ml-auto flex items-center gap-2">
        <button onClick={onReview} className="px-2.5 py-1 rounded text-[11.5px] font-medium"
                style={{ background: "var(--primary)", color: "var(--on-primary)" }}>
          review →
        </button>
        <button onClick={onDismiss}
                className="p-1 rounded row-hover"
                aria-label="dismiss">
          <Icon name="close" size={16} color="var(--on-surface-variant)"/>
        </button>
      </div>
    </div>
  );
}

function DocumentHUD({ docId, onClose, onNav }) {
  const content = window.HUD_CONTENT[docId];
  const doc = window.DOCUMENTS.find(d => d.id === docId);
  if (!doc) return null;

  const summary = content?.summary || [
    { kind: "legal",   text: "No AI summary yet for this document. Trigger summarization from the toolbar." },
    { kind: "action",  text: "—" },
    { kind: "finance", text: "—" },
  ];

  const title = content?.title || doc.title;
  const originator = content?.originator || originatorLabel(doc.lane);
  const ourAz = content?.ourAz || "ADV-024-A · AG 34 F 1120/26";
  const fileDate = content?.fileDate || doc.date;

  return (
    <>
      <div className="fixed inset-0 z-40" style={{ background: "rgba(0,0,0,0.35)" }} onClick={onClose}/>
      <aside className="fixed top-0 right-0 h-screen z-50 slide-in-right flex flex-col"
             style={{ width: 520, background: "var(--surface-container)", borderLeft: "1px solid var(--outline-variant)" }}>
        {/* HUD header */}
        <div className="px-5 py-3 flex items-center gap-2 border-b" style={{ borderColor: "var(--outline-variant)" }}>
          <span className="inline-block h-3 w-3 rounded-sm"
                style={{ background: originatorColorFor(doc.lane) }} />
          <span className="font-headline text-[11px] font-bold tracking-[0.12em]"
                style={{ color: "var(--on-surface-variant)" }}>DOCUMENT HUD</span>
          <div className="ml-auto flex items-center gap-1">
            <button onClick={() => onNav(-1)} className="p-1 rounded row-hover" title="Previous (←)">
              <Icon name="chevron-left" size={18}/>
            </button>
            <button onClick={() => onNav(1)} className="p-1 rounded row-hover" title="Next (→)">
              <Icon name="chevron-right" size={18}/>
            </button>
            <span className="v-divider mx-1" style={{ height: 18 }}/>
            <button onClick={onClose} className="p-1 rounded row-hover" title="Close (Esc)">
              <Icon name="close" size={18}/>
            </button>
          </div>
        </div>

        {/* Scroll area */}
        <div className="flex-1 overflow-auto custom-scrollbar">
          <div className="px-5 pt-4 pb-2">
            <h2 className="font-headline text-[20px] font-bold leading-tight mb-1"
                style={{ color: "var(--on-surface)" }}>{title}</h2>
            <div className="text-[12px] font-mono" style={{ color: "var(--on-surface-variant)" }}>
              {originator} · filed {fileDate} · {ourAz}
            </div>
          </div>

          {/* AI 3-bullet Management Summary */}
          <div className="mx-5 mt-3 p-4 rounded-md" style={{ background: "var(--surface-low)", border: "1px solid var(--outline-variant)" }}>
            <div className="flex items-center gap-2 mb-2.5">
              <Icon name="sparkle" size={14} color="var(--primary)"/>
              <span className="font-headline text-[10.5px] font-bold tracking-[0.14em]"
                    style={{ color: "var(--primary)" }}>MANAGEMENT SUMMARY</span>
              <span className="chip ml-auto" style={{ borderColor: "var(--primary)", color: "var(--primary)" }}>reviewed</span>
            </div>
            <ul className="space-y-2.5">
              {summary.map((s, i) => (
                <li key={i} className="flex items-start gap-2.5 text-[12.5px]">
                  <span className="font-mono text-[9.5px] uppercase tracking-wider px-1.5 py-0.5 rounded flex-shrink-0 mt-[1px]"
                        style={{
                          background: "var(--surface-container)",
                          color: s.kind === "legal" ? "var(--primary)" : s.kind === "action" ? "var(--amber)" : "var(--originator-own)",
                          minWidth: 58, textAlign: "center",
                        }}>
                    {s.kind}
                  </span>
                  <span style={{ color: "var(--on-surface)" }}>{s.text}</span>
                </li>
              ))}
            </ul>
          </div>

          {/* Key passages */}
          {content?.key_passages && (
            <div className="mx-5 mt-4">
              <div className="text-[10.5px] uppercase tracking-[0.12em] mb-2"
                   style={{ color: "var(--on-surface-variant)" }}>Key passages</div>
              {content.key_passages.map((p, i) => (
                <div key={i} className="mb-2 p-3 rounded-md text-[12.5px] italic leading-relaxed"
                     style={{
                       background: "var(--surface-low)",
                       borderLeft: `3px solid ${p.tag === "holding" ? "var(--primary)" : "var(--amber)"}`,
                       color: "var(--on-surface)",
                     }}>
                  <span className="chip mb-1.5 not-italic" style={{ borderColor: "var(--outline-variant)" }}>
                    {p.tag}
                  </span>
                  <div className="mt-1.5">„{p.p}"</div>
                </div>
              ))}
            </div>
          )}

          {/* Reaction bar */}
          <div className="mx-5 mt-4 p-3 rounded-md flex items-center gap-2"
               style={{ background: "var(--surface-low)", border: "1px solid var(--outline-variant)" }}>
            <span className="text-[11px]" style={{ color: "var(--on-surface-variant)" }}>React:</span>
            {[
              { e: "🚩", label: "Lies" },
              { e: "⚠", label: "Risk" },
              { e: "★", label: "Key" },
              { e: "✓", label: "Clear" },
              { e: "?", label: "Unclear" },
            ].map(r => (
              <button key={r.label}
                      className="flex items-center gap-1 text-[11px] px-2 py-1 rounded row-hover"
                      style={{ border: "1px solid var(--outline-variant)", color: "var(--on-surface)" }}>
                <span>{r.e}</span><span>{r.label}</span>
              </button>
            ))}
          </div>

          {/* Relationships */}
          {content?.relationships && (
            <div className="mx-5 mt-4 mb-5">
              <div className="text-[10.5px] uppercase tracking-[0.12em] mb-2"
                   style={{ color: "var(--on-surface-variant)" }}>Relationships</div>
              <div className="grid grid-cols-2 gap-3">
                <RelList label="INCOMING" items={content.relationships.incoming} />
                <RelList label="OUTGOING" items={content.relationships.outgoing} />
              </div>
            </div>
          )}
        </div>

        {/* Footer ask */}
        <div className="px-5 py-3 border-t flex items-center gap-2"
             style={{ borderColor: "var(--outline-variant)", background: "var(--surface-low)" }}>
          <Icon name="sparkle" size={14} color="var(--primary)"/>
          <button className="text-[12px]" style={{ color: "var(--primary)" }}>
            ask about this document
          </button>
          <div className="ml-auto flex items-center gap-2">
            <span className="text-[10.5px]" style={{ color: "var(--on-surface-variant)" }}>nav</span>
            <span className="kbd">←</span><span className="kbd">→</span>
            <span className="kbd">Esc</span>
          </div>
        </div>
      </aside>
    </>
  );
}

function RelList({ label, items }) {
  return (
    <div className="rounded-md p-2.5" style={{ background: "var(--surface-low)", border: "1px solid var(--outline-variant)" }}>
      <div className="text-[9.5px] font-mono tracking-wider mb-1.5" style={{ color: "var(--on-surface-variant)" }}>{label}</div>
      {items.length === 0 && <div className="text-[11.5px]" style={{ color: "var(--on-surface-variant)" }}>—</div>}
      {items.map((it, i) => (
        <div key={i} className="text-[11.5px] py-1" style={{ color: "var(--on-surface)" }}>
          <span className="cite">{it.docId}</span>
          <span style={{ color: "var(--on-surface-variant)" }}> {it.label}</span>
        </div>
      ))}
    </div>
  );
}

function originatorLabel(lane) {
  return { own: "You · Mandant", court: "Court · AG Hamburg", opposing: "Kanzlei Müller & Partner", third: "Third Party · Hamburg-Nord" }[lane] || "Unknown";
}
function originatorColorFor(lane) {
  return {
    court: "var(--originator-court)",
    opposing: "var(--originator-opposing)",
    own: "var(--originator-own)",
    third: "var(--originator-third)",
  }[lane] || "var(--originator-unknown)";
}

/* ---------------- Case AI Chat ---------------- */
function CaseChat({ onClose }) {
  const [messages, setMessages] = React.useState(window.CHAT_SEED);
  const [input, setInput] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const scrollRef = React.useRef();

  React.useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages, busy]);

  async function send() {
    const q = input.trim();
    if (!q) return;
    const newMsgs = [...messages, { role: "user", content: q }];
    setMessages(newMsgs);
    setInput("");
    setBusy(true);
    try {
      const ctx = "You are Sanctuary's case-scoped legal AI for ADV-024-A, Musterklage GmbH vs. XY at AG Hamburg. Respond concisely (≤120 words) with citations like [doc:d8] where helpful. The pending deadline is 30.04.2026 for a Stellungnahme to the third-party report.";
      const reply = await window.claude.complete({
        messages: [
          { role: "user", content: ctx + "\n\nQuestion: " + q }
        ]
      });
      setMessages(m => [...m, { role: "assistant", content: reply }]);
    } catch (e) {
      setMessages(m => [...m, { role: "assistant", content: "(Local model offline — showing cached response only.)" }]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="fixed top-0 right-0 h-screen z-40 slide-in-right flex flex-col"
           style={{ width: 420, background: "var(--surface-container)", borderLeft: "1px solid var(--outline-variant)" }}>
      <div className="px-4 py-3 flex items-center gap-2 border-b" style={{ borderColor: "var(--outline-variant)" }}>
        <Icon name="sparkle" size={16} color="var(--primary)"/>
        <span className="font-headline text-[12px] font-bold tracking-[0.1em]">CASE AI CHAT</span>
        <span className="chip ml-2">scope: case</span>
        <button onClick={onClose} className="ml-auto p-1 rounded row-hover">
          <Icon name="close" size={18}/>
        </button>
      </div>
      <div ref={scrollRef} className="flex-1 overflow-auto custom-scrollbar p-4 space-y-4">
        {messages.map((m, i) => (
          <div key={i} className={m.role === "user" ? "flex justify-end" : ""}>
            <div className={`max-w-[320px] px-3 py-2 rounded-md text-[12.5px] leading-relaxed whitespace-pre-wrap`}
                 style={m.role === "user"
                   ? { background: "var(--primary-container)", color: "var(--on-surface)" }
                   : { background: "var(--surface-low)", color: "var(--on-surface)", border: "1px solid var(--outline-variant)" }}>
              {m.content}
              {m.cites && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {m.cites.map(c => (
                    <span key={c} className="chip text-[10px]">📎 {c}</span>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}
        {busy && (
          <div className="flex items-center gap-2 text-[11px]" style={{ color: "var(--on-surface-variant)" }}>
            <Icon name="dots" size={12}/>
            retrieving from local embeddings…
          </div>
        )}
      </div>
      <div className="p-3 border-t" style={{ borderColor: "var(--outline-variant)" }}>
        <div className="flex items-center gap-2 rounded-md px-2 py-1.5"
             style={{ background: "var(--surface-low)", border: "1px solid var(--outline-variant)" }}>
          <Icon name="search" size={14} color="var(--on-surface-variant)"/>
          <input value={input} onChange={e => setInput(e.target.value)}
                 onKeyDown={e => e.key === "Enter" && send()}
                 placeholder="Ask anything about this case…"
                 className="flex-1 bg-transparent outline-none text-[12.5px]"
                 style={{ color: "var(--on-surface)" }}/>
          <button onClick={send} className="text-[11px] font-medium px-2 py-0.5 rounded"
                  style={{ background: "var(--primary)", color: "var(--on-primary)" }}>send</button>
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {["What did I flag 🚩 during triage?", "Summarize cost claims", "Argument on custody?"].map(s => (
            <button key={s} onClick={() => setInput(s)}
                    className="chip text-[10.5px]">{s}</button>
          ))}
        </div>
      </div>
    </aside>
  );
}

/* ---------------- Review Modal (delta) ---------------- */
function ReviewModal({ onClose, onOpenDoc }) {
  const items = window.DELTA.new_doc_ids.map(id => window.DOCUMENTS.find(d => d.id === id)).filter(Boolean);
  return (
    <>
      <div className="fixed inset-0 z-40" style={{ background: "rgba(0,0,0,0.5)" }} onClick={onClose}/>
      <div className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-[640px] rounded-lg fade-in"
           style={{ background: "var(--surface-container)", border: "1px solid var(--outline-variant)", boxShadow: "0 30px 60px rgba(0,0,0,0.4)" }}>
        <div className="px-5 py-3 flex items-center gap-2 border-b" style={{ borderColor: "var(--outline-variant)" }}>
          <Icon name="bolt" size={16} color="var(--amber)"/>
          <span className="font-headline text-[13px] font-bold">What's new since Apr 14</span>
          <button onClick={onClose} className="ml-auto p-1 rounded row-hover">
            <Icon name="close" size={18}/>
          </button>
        </div>
        <div className="p-4 space-y-3 max-h-[60vh] overflow-auto custom-scrollbar">
          {items.map(d => (
            <button key={d.id} onClick={() => { onOpenDoc(d.id); onClose(); }}
                    className="w-full text-left p-3 rounded-md row-hover flex items-start gap-3"
                    style={{ background: "var(--surface-low)", border: "1px solid var(--outline-variant)" }}>
              <span className="h-full w-1 rounded flex-shrink-0 self-stretch"
                    style={{ background: originatorColorFor(d.lane) }} />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-headline text-[13px] font-semibold">{d.title}</span>
                  <span className="chip">{d.sig}</span>
                </div>
                <div className="text-[11.5px] font-mono" style={{ color: "var(--on-surface-variant)" }}>{d.date} · {originatorLabel(d.lane)}</div>
                <div className="mt-1.5 text-[12px]" style={{ color: "var(--on-surface)" }}>
                  {d.id === "d8" && "Grants PKH without contributions. New deadline extracted: Apr 30."}
                  {d.id === "d10" && "Opposing counsel seeks §91 cost reimbursement (1.240 €). No response filed."}
                  {d.id === "d7" && "Third-party report relayed via court — requires Stellungnahme."}
                </div>
              </div>
            </button>
          ))}
        </div>
      </div>
    </>
  );
}

/* ---------------- Tweaks panel ---------------- */
function TweaksPanel({ open, tweaks, setTweaks }) {
  if (!open) return null;
  const Row = ({ label, children }) => (
    <div className="mb-3">
      <div className="text-[10.5px] uppercase tracking-[0.1em] mb-1.5"
           style={{ color: "var(--on-surface-variant)" }}>{label}</div>
      {children}
    </div>
  );
  const Seg = ({ value, setValue, options }) => (
    <div className="flex rounded-md overflow-hidden" style={{ border: "1px solid var(--outline-variant)" }}>
      {options.map(o => (
        <button key={o.v} onClick={() => setValue(o.v)}
                className="flex-1 text-[11px] py-1.5"
                style={value === o.v
                  ? { background: "var(--primary-container)", color: "var(--primary)", fontWeight: 600 }
                  : { color: "var(--on-surface-variant)" }}>
          {o.l}
        </button>
      ))}
    </div>
  );
  return (
    <div className="tweaks fade-in">
      <div className="flex items-center gap-2 mb-3">
        <Icon name="tune" size={14} color="var(--primary)"/>
        <span className="font-headline text-[12px] font-bold tracking-wide">Tweaks</span>
      </div>
      <Row label="Theme">
        <Seg value={tweaks.theme} setValue={v => setTweaks({ ...tweaks, theme: v })}
             options={[{ v: "dark", l: "Dark" }, { v: "light", l: "Light" }]}/>
      </Row>
      <Row label="Density">
        <Seg value={tweaks.density} setValue={v => setTweaks({ ...tweaks, density: v })}
             options={[{ v: "compact", l: "Compact" }, { v: "default", l: "Default" }, { v: "comfortable", l: "Roomy" }]}/>
      </Row>
      <Row label="Graph filter">
        <Seg value={tweaks.graphFilter} setValue={v => setTweaks({ ...tweaks, graphFilter: v })}
             options={[{ v: "critical", l: "Critical" }, { v: "significant+", l: "Sig+" }, { v: "all", l: "All" }]}/>
      </Row>
      <Row label="Active view">
        <Seg value={tweaks.activeView} setValue={v => setTweaks({ ...tweaks, activeView: v })}
             options={[{ v: "graph", l: "Graph" }, { v: "timeline", l: "Timeline" }, { v: "truth", l: "Truth" }, { v: "fin", l: "Fin" }]}/>
      </Row>
    </div>
  );
}

Object.assign(window, { DeltaBanner, DocumentHUD, CaseChat, ReviewModal, TweaksPanel });

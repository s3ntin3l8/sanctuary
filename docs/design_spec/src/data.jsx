// Seed data mirroring the spec's ADV-024-A example case.

const CASE = {
  id: "ADV-024-A",
  title: "Musterklage GmbH vs. XY",
  status: "active",
  court: "AG Hamburg",
  tempoDays: 12,
  lastViewedAt: "2026-04-14T18:32:00Z",
  aiBriefUpdatedAt: "4 hours ago",
  activeProceedingId: "p1",
};

const PROCEEDINGS = [
  { id: "p1", court: "AG Hamburg",  az: "34 F 1120/26",   level: "Amtsgericht",   docs: 41 },
  { id: "p2", court: "LG Hamburg",  az: "7 O 284/25",     level: "Landgericht",   docs: 18 },
  { id: "p3", court: "OLG Hamburg", az: "2 UF 88/26",     level: "Oberlandesgericht", docs: 6 },
];

const AI_BRIEF = {
  status_line: "Active — awaiting your Stellungnahme to the third-party report from 28.03.",
  key_risks: [
    { id: "r1", label: "Frist Apr 30", sub: "12 days", severity: "critical" },
    { id: "r2", label: "§91 costs", sub: "opposing side seeking 1.240 €", severity: "near" },
  ],
  open_threads: [
    { thread: "Third-party report", description: "awaiting your response" },
  ],
  recent_development: "Beschluss PKH granted 02.04.",
};

const PARTIES = [
  { key: "court",    label: "Court",          name: "AG Hamburg",                color: "court"    },
  { key: "opposing", label: "Opposing",       name: "Kanzlei Müller & Partner",  color: "opposing" },
  { key: "third",    label: "Third Party",      name: "Third Party · Hamburg-Nord",         color: "third"    },
  { key: "own",      label: "You",            name: "Mandant",                   color: "own"      },
];

const FINANCIALS = {
  total_eur: 1690,
  last_delta: { amount: 450, date: "Apr 02", label: "Gerichtskostenvorschuss" },
  breakdown: [
    { label: "Court fees (GKG)",      amount: 747, status: "paid"        },
    { label: "Counsel fees (RVG 3100/3104)", amount: 793, status: "accrued" },
    { label: "Dispatch & copies",     amount: 150, status: "paid"        },
  ],
};

// Swim-lane positions: col centers (lane x) and row ys for dates
const LANES = [
  { key: "own",      label: "YOU",            color: "own"      },
  { key: "court",    label: "COURT",          color: "court"    },
  { key: "opposing", label: "OPPOSING",       color: "opposing" },
  { key: "third",    label: "THIRD PARTY",      color: "third"    },
];

// Graph nodes — 10 docs showing Russian Doll court relay (Beglaubigung bundle)
const DOCUMENTS = [
  { id: "d1", lane: "own",      row: 0,  title: "Klage eingereicht",           date: "2026-02-18", sig: "critical",      role: "origin",  thread: false },
  { id: "d2", lane: "court",    row: 0,  title: "Eingangsbestätigung",         date: "2026-02-19", sig: "administrative",role: "ack",     thread: false, relayParent: null },
  { id: "d3", lane: "court",    row: 1,  title: "Kostenvorschussanf.",         date: "2026-02-24", sig: "significant",   role: "order",   thread: false },
  { id: "d4", lane: "own",      row: 2,  title: "Einzahlung GKG 747 €",        date: "2026-03-02", sig: "administrative",role: "payment", thread: false },
  { id: "d5", lane: "court",    row: 3,  title: "Beschluss Zustellung",        date: "2026-03-06", sig: "significant",   role: "order",   thread: false },

  // Beglaubigung bundle — court relay, three nested docs
  { id: "bundle", lane: "court", row: 4, title: "Beglaubigung",                date: "2026-03-12", sig: "significant",   role: "relay",   thread: false,
    children: [
      { id: "d6",  title: "Klageerwiderung",  origin: "opposing", date: "2026-03-10" },
      { id: "d7",  title: "Third-party report",       origin: "third",    date: "2026-03-11" },
    ]
  },

  { id: "d8", lane: "court",    row: 6,  title: "Beschluss PKH gewährt",       date: "2026-04-02", sig: "critical",      role: "order",   thread: false, highlight: true },
  { id: "d9", lane: "own",      row: 7,  title: "Stellungnahme (offen)",       date: "Apr 30",     sig: "critical",      role: "pending", thread: true,  ghost: true },
  { id: "d10",lane: "opposing", row: 5,  title: "Antrag §91",                  date: "2026-03-28", sig: "significant",   role: "filing",  thread: true },
];

// Edges: { from, to, kind }
const EDGES = [
  { from: "d1", to: "d2", kind: "ack" },
  { from: "d3", to: "d4", kind: "action" },
  { from: "d5", to: "bundle", kind: "deliver" },
  { from: "d6", to: "bundle", kind: "relay" },   // opposing → court
  { from: "d7", to: "bundle", kind: "relay" },   // third-party → court
  { from: "bundle", to: "d9", kind: "requires" },
  { from: "d8", to: "d9", kind: "requires" },
  { from: "d10", to: "d8", kind: "reference" },
];

const ACTION_ITEMS = [
  { id: "a1", urgency: "critical", date: "Apr 30", days: 12, title: "Stellungnahme zu Beschluss vom 02.04", badge: "deadline",    sourceDocId: "d8" },
  { id: "a2", urgency: "near",     date: "Jun 15", days: 58, title: "Verhandlungstermin AG Hamburg",        badge: "court date",  sourceDocId: "d5" },
  { id: "a3", urgency: "clock",    date: "",       days: null, title: "typical: hearing follows Klageerwiderung by 4–8 months", clock: "window Jul–Nov 2026", badge: "case clock" },
];

const DELTA = {
  new_doc_ids: ["d8", "d10", "d7"],
  new_actions: 1,
  since: "Apr 14",
};

// Document HUD sample content (for d8)
const HUD_CONTENT = {
  d8: {
    title: "Beschluss — PKH gewährt",
    originator: "AG Hamburg · Kammer 34",
    fileDate: "2026-04-02",
    ourAz: "ADV-024-A · AG 34 F 1120/26",
    summary: [
      { kind: "legal",    text: "Prozesskostenhilfe is granted without monthly contributions. The court affirms the merits threshold under §114 ZPO is met." },
      { kind: "action",   text: "Stellungnahme zum Bericht due by 30.04.2026 (§118 Abs. 2 ZPO, 4-Wochen-Frist)." },
      { kind: "finance",  text: "No cost delta — PKH suspends advance obligations; §91 reimbursement question deferred until Endurteil." },
    ],
    key_passages: [
      { p: "Dem Antragsteller wird für den ersten Rechtszug Prozesskostenhilfe ohne Ratenzahlung bewilligt.", tag: "holding" },
      { p: "Zur Stellungnahme auf den Bericht der Dritten Stelle wird eine Frist von vier Wochen ab Zustellung gesetzt.", tag: "deadline" },
    ],
    relationships: {
      incoming: [
        { docId: "d10", label: "references — Antrag §91 (opposing)" },
      ],
      outgoing: [
        { docId: "d9", label: "requires — Stellungnahme (pending)" },
      ],
    },
  }
};

// Chat seed
const CHAT_SEED = [
  {
    role: "user",
    content: "Which opposing statements haven't been responded to yet?",
  },
  {
    role: "assistant",
    content: "Two items are still awaiting your response:\n\n1. **Third-party report** of 11.03.2026 — relayed via court on 12.03. Your Stellungnahme is due by **30.04.2026**.\n\n2. **Antrag §91** filed by opposing counsel on 28.03.2026 — no response lodged yet; no court-set deadline, but likely to surface at the hearing.",
    cites: ["d7", "d10"]
  }
];

Object.assign(window, {
  CASE, PROCEEDINGS, AI_BRIEF, PARTIES, FINANCIALS,
  LANES, DOCUMENTS, EDGES, ACTION_ITEMS, DELTA, HUD_CONTENT, CHAT_SEED
});

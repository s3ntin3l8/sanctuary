// Home page data — cross-case aggregates

const NOW = new Date("2026-04-16T08:30:00");
const LAST_VISIT_HOURS_AGO = 6;

window.HOME = {
  user: { name: "Björn", email: "bjoern@kanzlei.de" },
  now: NOW,
  lastVisit: new Date(NOW.getTime() - LAST_VISIT_HOURS_AGO * 3600 * 1000),
  lastVisitLabel: "6 hours ago",
};

// TODAY panel — deadlines & court dates within 30 days
window.TODAY_ITEMS = [
  {
    id: "a1",
    kind: "deadline",          // deadline | court_date | task
    urgency: "critical",       // critical (≤7d or overdue) | near (8-30d)
    dueDate: "2026-04-18",
    daysUntil: 2,
    title: "Kostenvorschuss fällig",
    subtitle: "Gerichtskasse Hamburg · §12 GKG",
    caseId: "ADV-031-B",
    caseTitle: "Vane vs. Vane",
    amount: "240 €",
  },
  {
    id: "a2",
    kind: "deadline",
    urgency: "critical",
    dueDate: "2026-04-30",
    daysUntil: 14,
    title: "Stellungnahme zu Beschluss vom 02.04.",
    subtitle: "AG Hamburg · 302 C 142/25",
    caseId: "ADV-024-A",
    caseTitle: "Musterklage GmbH vs. XY",
  },
  {
    id: "a3",
    kind: "court_date",
    urgency: "near",
    dueDate: "2026-06-15",
    daysUntil: 60,
    title: "Verhandlungstermin AG Hamburg",
    subtitle: "Saal 212 · 09:30 Uhr",
    caseId: "ADV-024-A",
    caseTitle: "Musterklage GmbH vs. XY",
  },
];

// AWAITING TRIAGE — IngestBatch rows with status != completed
window.TRIAGE_BUNDLES = [
  {
    id: "b1",
    docCount: 5,
    receivedDate: "2026-04-14",
    receivedLabel: "14 Apr",
    sourceHint: "anwalt@kanzlei.de",
    sourceKind: "email",
    suggestedCaseId: "ADV-024-A",
    confidence: "suggested",   // confirmed | suggested | unknown
    preview: "Schriftsatz Gegenseite + 4 Anlagen",
  },
  {
    id: "b2",
    docCount: 3,
    receivedDate: "2026-04-12",
    receivedLabel: "12 Apr",
    sourceHint: "Scan-Ordner",
    sourceKind: "scan",
    suggestedCaseId: null,
    confidence: "unknown",
    preview: "3 Seiten OCR · kein Absender erkannt",
  },
];

// DELTA — "Since your last visit"
window.DELTA_CASES = [
  {
    caseId: "ADV-024-A",
    newDocs: 3,
    newActions: 1,
    maxSignificance: "significant",
    docTitles: ["Schriftsatz Gegenseite v. 14.04.", "Anlage K7 (Rechnung)", "Deckungszusage Rechtsschutz"],
  },
  {
    caseId: "ADV-092-B",
    newDocs: 1,
    newActions: 0,
    maxSignificance: "informational",
    docTitles: ["Third-party report v. 15.04."],
  },
];

// SIGNALS — ambient alerts
window.SIGNALS = [
  {
    id: "s1",
    kind: "dormancy",
    severity: "warn",
    title: "ADV-031-B quiet 6 months",
    detail: "longer than typical for OLG (median 11 weeks between acts)",
    action: "check",
    link: "case",
  },
  {
    id: "s2",
    kind: "case_clock",
    severity: "info",
    title: "ADV-024-A entering typical hearing window",
    detail: "Jul–Nov window based on 847 similar AG Hamburg cases",
    action: "details",
    link: "caseclock",
  },
  {
    id: "s3",
    kind: "gmail",
    severity: "warn",
    title: "Gmail sync: auth expired",
    detail: "last successful sync 2 days ago",
    action: "reconnect",
    link: "settings",
  },
  {
    id: "s4",
    kind: "ingest_failed",
    severity: "warn",
    title: "2 documents stuck in ingest_status=FAILED",
    detail: "OCR timeout on scan_0412_a.pdf, scan_0412_b.pdf",
    action: "review",
    link: "triage",
  },
];

// ACTIVE CASES — baseline grid
window.ACTIVE_CASES = [
  {
    id: "ADV-024-A",
    title: "Musterklage GmbH vs. XY",
    proceeding: "AG Hamburg",
    status: "active",
    statusLine: "Awaiting Stellungnahme — fr. Apr 30",
    nextAction: { kind: "deadline", label: "Frist Apr 30", daysUntil: 14 },
    exposure: "1.690 €",
    newDocs: 3,
    daysSinceActivity: 2,
    tier: "delta",            // delta | imminent | dormant | normal
  },
  {
    id: "ADV-031-B",
    title: "Vane vs. Vane",
    proceeding: "OLG Hamburg",
    status: "active",
    statusLine: "Dormant — no activity Oct 2025 → now",
    nextAction: { kind: "deadline", label: "Kostenvorschuss Apr 18", daysUntil: 2 },
    exposure: "840 €",
    newDocs: 0,
    daysSinceActivity: 180,
    tier: "imminent",
    dormant: true,
  },
  {
    id: "ADV-019-C",
    title: "Mercury Tech IP Dispute",
    proceeding: "LG Berlin",
    status: "active",
    statusLine: "Settlement talks — counter-offer pending",
    nextAction: null,
    exposure: "12.400 €",
    newDocs: 0,
    daysSinceActivity: 22,
    tier: "normal",
  },
  {
    id: "ADV-055-D",
    title: "Smith vs. City Council",
    proceeding: "AG Hamburg",
    status: "active",
    statusLine: "Discovery — awaiting Akteneinsicht",
    nextAction: null,
    exposure: "3.200 €",
    newDocs: 1,
    daysSinceActivity: 5,
    tier: "delta",
  },
  {
    id: "ADV-071-E",
    title: "Hansen — Kündigungsschutz",
    proceeding: "ArbG Hamburg",
    status: "active",
    statusLine: "Gütetermin scheduled — 22.05.",
    nextAction: { kind: "court_date", label: "Gütetermin May 22", daysUntil: 36 },
    exposure: "6.800 €",
    newDocs: 0,
    daysSinceActivity: 11,
    tier: "normal",
  },
  {
    id: "ADV-088-F",
    title: "Schmidt — Mietkaution",
    proceeding: "AG Altona",
    status: "active",
    statusLine: "Klage eingereicht — Zustellung abwarten",
    nextAction: null,
    exposure: "1.240 €",
    newDocs: 0,
    daysSinceActivity: 34,
    tier: "normal",
  },
];

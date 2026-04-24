from app.models.database import CaseStatus, CostCategory, CostStatus, OriginatorType

ORIGINATOR_COLORS = {
    OriginatorType.COURT: "#0369A1",
    OriginatorType.OPPOSING: "#B91C1C",
    OriginatorType.OWN: "#047857",
    OriginatorType.THIRD_PARTY: "#C2410C",
    OriginatorType.UNKNOWN: "#64748b",
}

ORIGINATOR_ICONS = {
    OriginatorType.COURT: "gavel",
    OriginatorType.OPPOSING: "warning",
    OriginatorType.OWN: "shield",
    OriginatorType.THIRD_PARTY: "groups",
    OriginatorType.UNKNOWN: "help_outline",
}

CASE_STATUS_META = {
    CaseStatus.INTAKE: {
        "label": "Intake",
        "color": "bg-slate-100 text-slate-700",
        "dot": "bg-slate-400",
    },
    CaseStatus.DISCOVERY: {
        "label": "Discovery",
        "color": "bg-blue-50 text-blue-700",
        "dot": "bg-blue-500",
    },
    CaseStatus.PRE_TRIAL: {
        "label": "Pre-Trial",
        "color": "bg-amber-50 text-amber-700",
        "dot": "bg-amber-500",
    },
    CaseStatus.TRIAL: {
        "label": "Trial",
        "color": "bg-rose-50 text-rose-700",
        "dot": "bg-rose-500",
    },
    CaseStatus.POST_TRIAL: {
        "label": "Post-Trial",
        "color": "bg-purple-50 text-purple-700",
        "dot": "bg-purple-500",
    },
    CaseStatus.CLOSED: {
        "label": "Closed",
        "color": "bg-slate-100 text-slate-500",
        "dot": "bg-slate-300",
    },
}

COST_CATEGORY_META = {
    CostCategory.GERICHTSKOSTEN: {
        "label": "Court Fees (Gerichtskosten)",
        "short": "GKG",
        "color": "bg-originator-court/10 text-originator-court",
    },
    CostCategory.ANWALTSKOSTEN: {
        "label": "Own Counsel (Anwaltskosten)",
        "short": "RVG",
        "color": "bg-originator-own/10 text-originator-own",
    },
    CostCategory.ANWALTSKOSTEN_GEGNER: {
        "label": "Opposing Counsel (Gegner)",
        "short": "§91 ZPO",
        "color": "bg-originator-opposing/10 text-originator-opposing",
    },
    CostCategory.SACHVERSTAENDIGER: {
        "label": "Expert Witness (Sachverständiger)",
        "short": "JVEG",
        "color": "bg-amber-500/10 text-amber-600",
    },
    CostCategory.VORSCHUSS: {
        "label": "Advance Payment (Kostenvorschuss)",
        "short": "GKV",
        "color": "bg-primary/10 text-primary",
    },
    CostCategory.VOLLSTRECKUNG: {
        "label": "Enforcement (Vollstreckung)",
        "short": "ZVG",
        "color": "bg-error/10 text-error",
    },
    CostCategory.AUSLAGEN: {
        "label": "Out-of-Pocket (Auslagen)",
        "short": "RVG",
        "color": "bg-surface-container-highest text-on-surface-variant",
    },
    CostCategory.SONSTIGES: {
        "label": "Other (Sonstiges)",
        "short": "—",
        "color": "bg-surface-container-highest text-on-surface-variant",
    },
}

COST_STATUS_META = {
    CostStatus.OFFEN: {
        "label": "Open",
        "color": "bg-originator-opposing/10 text-originator-opposing",
    },
    CostStatus.BEZAHLT: {
        "label": "Paid",
        "color": "bg-originator-own/10 text-originator-own",
    },
    CostStatus.ERSTATTET: {
        "label": "Reimbursed",
        "color": "bg-primary/10 text-primary",
    },
    CostStatus.TEILWEISE: {
        "label": "Partial",
        "color": "bg-amber-500/10 text-amber-600",
    },
    CostStatus.STRITTIG: {"label": "Disputed", "color": "bg-error/10 text-error"},
}

REVIEW_FIELD_LABELS = {
    "missing_case_id": {"label": "Case ID", "icon": "folder", "field": "case_id"},
    "missing_originator": {
        "label": "Originator Type",
        "icon": "person",
        "field": "originator_type",
    },
    "missing_sender": {"label": "Sender / Source", "icon": "mail", "field": "sender"},
    "missing_issued_date": {
        "label": "Issued Date",
        "icon": "calendar_today",
        "field": "issued_date",
    },
    "missing_received_date": {
        "label": "Received Date",
        "icon": "calendar_today",
        "field": "received_date",
    },
    "missing_parent": {
        "label": "Parent Relationship",
        "icon": "account_tree",
        "field": "parent_id",
    },
    "missing_title": {"label": "Document Title", "icon": "title", "field": "title"},
    "missing_content": {
        "label": "Document Content",
        "icon": "article",
        "field": "content",
    },
    "conversion_failed": {
        "label": "Conversion Failed",
        "icon": "error_outline",
        "field": "content",
    },
}

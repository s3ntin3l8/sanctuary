from app.models.database import CaseStatus
from app.models.enums import SignificanceTier

# Lower index = higher significance. Used for picking the lead doc in a bundle
# (triage_service + triage_view both rank by this order; keep as a single source).
SIG_ORDER: dict[SignificanceTier, int] = {
    SignificanceTier.CRITICAL: 0,
    SignificanceTier.SIGNIFICANT: 1,
    SignificanceTier.INFORMATIONAL: 2,
    SignificanceTier.ADMINISTRATIVE: 3,
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

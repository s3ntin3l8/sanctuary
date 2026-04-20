from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.database import Case, Document
from app.models.enums import CaseStatus, IngestStatus, ProceedingStatus

DORMANCY_THRESHOLD_DAYS = 90


def get_signals(db: Session) -> list[dict[str, Any]]:
    """Aggregate ambient signals and alerts across all cases."""
    signals = []

    # 1. Dormancy Alerts
    signals.extend(_get_dormancy_signals(db))

    # 2. Ingest Failures
    signals.extend(_get_ingest_failure_signals(db))

    # 3. System Status & Health
    signals.extend(_get_system_health_signals(db))

    # 4. Case Clock Signals
    signals.extend(_get_case_clock_signals(db))

    return signals


def _get_system_health_signals(db: Session) -> list[dict[str, Any]]:
    signals = []

    # Gmail Sync Check (Simulated for Phase 5)
    # In a real implementation, we would check the UserSettings for an expired OAuth token
    # signals.append({
    #     "id": "gmail-sync-auth",
    #     "kind": "gmail",
    #     "severity": "warn",
    #     "title": "Gmail sync: auth expired",
    #     "detail": "Last successful sync 2 days ago. Reconnect to resume ingestion.",
    #     "action": "reconnect",
    #     "link": "/settings"
    # })

    # AI Provider Check (Basic reachability placeholder)
    import requests

    from app.config import AI_BASE_URL

    try:
        # Quick health check for Ollama/LM Studio if local
        if "localhost" in AI_BASE_URL or "127.0.0.1" in AI_BASE_URL:
            # Short timeout to avoid blocking Home page
            resp = requests.get(AI_BASE_URL, timeout=0.5)
            if resp.status_code >= 500:
                raise Exception("Provider error")
    except Exception:
        signals.append(
            {
                "id": "ai-provider-offline",
                "kind": "ai_provider",
                "severity": "warn",
                "title": "AI Backend unreachable",
                "detail": f"Connection failed to {AI_BASE_URL}. Automatic analysis paused.",
                "action": "check settings",
                "link": "/settings",
            }
        )

    return signals


def _get_case_clock_signals(db: Session) -> list[dict[str, Any]]:
    signals = []

    # Case Clock Window (Simulated example)
    # "ADV-024-A entering typical hearing window (Jul–Nov)"
    # This would be derived from CaseClockService

    return signals


def _get_dormancy_signals(db: Session) -> list[dict[str, Any]]:
    signals = []
    now = datetime.now()

    # Only check active cases
    active_cases = db.query(Case).filter(Case.status != CaseStatus.CLOSED).all()
    for case in active_cases:
        # Check active proceedings within the case
        for proc in case.proceedings:
            if proc.status == ProceedingStatus.ACTIVE:
                # Find the most recent document for this proceeding
                last_doc = (
                    db.query(Document)
                    .filter(Document.proceeding_id == proc.id)
                    .order_by(Document.created_at.desc())
                    .first()
                )
                last_activity = (
                    last_doc.created_at
                    if last_doc
                    else (proc.started_at or proc.created_at)
                )

                if last_activity:
                    days_silent = (now - last_activity).days
                    if days_silent > DORMANCY_THRESHOLD_DAYS:
                        signals.append(
                            {
                                "id": f"dormancy-{proc.id}",
                                "kind": "dormancy",
                                "severity": "warn",
                                "title": f"{case.id} quiet {days_silent} days",
                                "detail": f"{proc.court_name} ({proc.az_court or 'No Az'}) has had no activity for {days_silent} days.",
                                "action": "check",
                                "link": f"/cases/{case.id}",
                            }
                        )
    return signals


def _get_ingest_failure_signals(db: Session) -> list[dict[str, Any]]:
    signals = []
    failed_docs = (
        db.query(Document).filter(Document.ingest_status == IngestStatus.FAILED).all()
    )
    if failed_docs:
        signals.append(
            {
                "id": "ingest-failed",
                "kind": "ingest_failed",
                "severity": "warn",
                "title": f"{len(failed_docs)} document{'s' if len(failed_docs) > 1 else ''} stuck in ingest_status=FAILED",
                "detail": "Pipeline errors detected in triage queue.",
                "action": "review",
                "link": "/triage",
            }
        )
    return signals

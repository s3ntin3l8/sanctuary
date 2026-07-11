"""Unit tests for user_settings_service (per-user) + the global AppSettings half."""

from datetime import datetime

import pytest

from app.models.database import AppSettings, Case, Document
from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
from app.services.user_settings_service import (
    count_new_since,
    get_last_viewed,
    mark_viewed,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def app_row(db_session) -> AppSettings:
    """The global AppSettings singleton with an empty settings_json.

    AppSettings is a singleton; the bootstrap-admin pin may already have created
    the row, so get-or-create it rather than inserting a second one.
    """
    row = db_session.query(AppSettings).first()
    if row is None:
        row = AppSettings(settings_json={})
        db_session.add(row)
    else:
        row.settings_json = {}
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def case_a(db_session) -> Case:
    case = Case(
        id="USS-TEST-A",
        title="Case A",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()
    db_session.refresh(case)
    return case


@pytest.fixture
def case_b(db_session) -> Case:
    case = Case(
        id="USS-TEST-B",
        title="Case B",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db_session.add(case)
    db_session.commit()
    db_session.refresh(case)
    return case


# ── get_last_viewed (per-user) ──────────────────────────────────────────────────


@pytest.mark.unit
def test_get_last_viewed_no_settings_row(db_session, sample_user):
    """Returns None when the user has no settings row yet."""
    assert get_last_viewed("SOME-CASE", db_session, sample_user.id) is None


@pytest.mark.unit
def test_get_last_viewed_no_last_viewed_key(db_session, sample_user):
    """Returns None when there is no last_viewed entry for this case."""
    assert get_last_viewed("NONEXISTENT-CASE", db_session, sample_user.id) is None


@pytest.mark.unit
def test_get_last_viewed_after_mark_viewed(db_session, sample_user, case_a):
    fixed_now = datetime(2026, 4, 19, 10, 0, 0)  # naive UTC
    mark_viewed(case_a.id, db_session, sample_user.id, now=fixed_now)

    result = get_last_viewed(case_a.id, db_session, sample_user.id)
    assert result == fixed_now


@pytest.mark.unit
def test_mark_viewed_creates_row(db_session, sample_user, case_a):
    """mark_viewed creates the per-user settings row on demand."""
    mark_viewed(case_a.id, db_session, sample_user.id)
    assert get_last_viewed(case_a.id, db_session, sample_user.id) is not None


@pytest.mark.unit
def test_per_user_isolation(db_session, sample_user, case_a):
    """One user's last-viewed does not leak to another user."""
    from app.services import auth_service

    other = auth_service.create_user(
        db_session, email="other@example.com", password="password123"
    )
    db_session.commit()
    mark_viewed(case_a.id, db_session, sample_user.id, now=datetime(2026, 4, 1))
    assert get_last_viewed(case_a.id, db_session, sample_user.id) is not None
    assert get_last_viewed(case_a.id, db_session, other.id) is None


# ── mark_viewed — multiple cases don't collide ────────────────────────────────


@pytest.mark.unit
def test_multiple_cases_coexist(db_session, sample_user, case_a, case_b):
    time_a = datetime(2026, 4, 1, 8, 0, 0)
    time_b = datetime(2026, 4, 15, 12, 0, 0)

    mark_viewed(case_a.id, db_session, sample_user.id, now=time_a)
    mark_viewed(case_b.id, db_session, sample_user.id, now=time_b)

    assert get_last_viewed(case_a.id, db_session, sample_user.id) == time_a
    assert get_last_viewed(case_b.id, db_session, sample_user.id) == time_b


@pytest.mark.unit
def test_mark_viewed_overwrites_previous(db_session, sample_user, case_a):
    first = datetime(2026, 3, 1, 0, 0, 0)
    second = datetime(2026, 4, 19, 9, 0, 0)

    mark_viewed(case_a.id, db_session, sample_user.id, now=first)
    mark_viewed(case_a.id, db_session, sample_user.id, now=second)

    assert get_last_viewed(case_a.id, db_session, sample_user.id) == second


# ── count_new_since (global query) ───────────────────────────────────────────────


@pytest.mark.unit
def test_count_new_since_none_returns_zero(db_session, case_a):
    assert count_new_since(case_a.id, None, db_session) == 0


@pytest.mark.unit
def test_count_new_since_excludes_old_documents(db_session, sample_user, case_a):
    mark_viewed(
        case_a.id, db_session, sample_user.id, now=datetime(2026, 4, 10, 12, 0, 0)
    )

    db_session.add_all(
        [
            Document(
                title="Old Doc",
                case_id=case_a.id,
                originator_type=OriginatorType.COURT,
                ingest_date=datetime(2026, 4, 9, 0, 0, 0),
            ),
            Document(
                title="New Doc",
                case_id=case_a.id,
                originator_type=OriginatorType.COURT,
                ingest_date=datetime(2026, 4, 11, 0, 0, 0),
            ),
        ]
    )
    db_session.commit()

    since = get_last_viewed(case_a.id, db_session, sample_user.id)
    assert count_new_since(case_a.id, since, db_session) == 1


@pytest.mark.unit
def test_count_new_since_counts_only_this_case(db_session, case_a, case_b):
    since = datetime(2026, 4, 1, 0, 0, 0)
    db_session.add_all(
        [
            Document(
                title="Doc for A",
                case_id=case_a.id,
                originator_type=OriginatorType.OWN,
                ingest_date=datetime(2026, 4, 5, 0, 0, 0),
            ),
            Document(
                title="Doc for B",
                case_id=case_b.id,
                originator_type=OriginatorType.OWN,
                ingest_date=datetime(2026, 4, 5, 0, 0, 0),
            ),
        ]
    )
    db_session.commit()
    assert count_new_since(case_a.id, since, db_session) == 1


@pytest.mark.unit
def test_count_new_since_zero_when_no_new_docs(db_session, case_a):
    since = datetime(2026, 4, 20, 0, 0, 0)
    db_session.add(
        Document(
            title="Old Doc",
            case_id=case_a.id,
            originator_type=OriginatorType.COURT,
            ingest_date=datetime(2026, 4, 1, 0, 0, 0),
        )
    )
    db_session.commit()
    assert count_new_since(case_a.id, since, db_session) == 0


# ── Stale-job recovery (global AppSettings) ──────────────────────────────────────


def _stale_iso(minutes_ago: int) -> str:
    from datetime import timedelta

    from app.core.timezone import naive_utc_now

    return (naive_utc_now() - timedelta(minutes=minutes_ago)).isoformat()


def test_recover_stale_reindex_job_flips_old_running(app_row, db_session):
    from app.services.user_settings_service import recover_stale_reindex_job

    app_row.settings_json = {
        "reindex_job": {
            "status": "running",
            "total": 100,
            "reindexed": 5,
            "failed": 0,
            "started_at": _stale_iso(90),
            "ended_at": None,
            "embed_dim": 768,
            "error": None,
        }
    }
    db_session.commit()

    flipped = recover_stale_reindex_job(db_session)
    db_session.commit()

    assert flipped is True
    db_session.refresh(app_row)
    job = app_row.settings_json["reindex_job"]
    assert job["status"] == "failed"
    assert "stale" in (job.get("error") or "")
    assert job["ended_at"] is not None


def test_recover_stale_reindex_job_skips_fresh_running(app_row, db_session):
    from app.services.user_settings_service import recover_stale_reindex_job

    app_row.settings_json = {
        "reindex_job": {
            "status": "running",
            "started_at": _stale_iso(5),
            "ended_at": None,
        }
    }
    db_session.commit()

    assert recover_stale_reindex_job(db_session) is False
    db_session.refresh(app_row)
    assert app_row.settings_json["reindex_job"]["status"] == "running"


def test_recover_stale_reindex_job_skips_done(app_row, db_session):
    from app.services.user_settings_service import recover_stale_reindex_job

    app_row.settings_json = {
        "reindex_job": {
            "status": "done",
            "started_at": _stale_iso(120),
            "ended_at": _stale_iso(110),
        }
    }
    db_session.commit()

    assert recover_stale_reindex_job(db_session) is False
    db_session.refresh(app_row)
    assert app_row.settings_json["reindex_job"]["status"] == "done"


def test_recover_stale_reindex_job_handles_missing_started_at(app_row, db_session):
    from app.services.user_settings_service import recover_stale_reindex_job

    app_row.settings_json = {"reindex_job": {"status": "running"}}
    db_session.commit()

    assert recover_stale_reindex_job(db_session) is False
    db_session.refresh(app_row)
    assert app_row.settings_json["reindex_job"]["status"] == "running"


def test_recover_stale_dedup_jobs_flips_multiple_cases(app_row, db_session):
    from app.services.user_settings_service import recover_stale_dedup_jobs

    app_row.settings_json = {
        "dedup_jobs": {
            "CASE-A": {
                "status": "running",
                "started_at": _stale_iso(90),
                "ended_at": None,
            },
            "CASE-B": {
                "status": "running",
                "started_at": _stale_iso(120),
                "ended_at": None,
            },
            "CASE-C": {
                "status": "running",
                "started_at": _stale_iso(5),  # fresh — should NOT flip
                "ended_at": None,
            },
        }
    }
    db_session.commit()

    flipped = recover_stale_dedup_jobs(db_session)
    db_session.commit()

    assert set(flipped) == {"CASE-A", "CASE-B"}
    db_session.refresh(app_row)
    jobs = app_row.settings_json["dedup_jobs"]
    assert jobs["CASE-A"]["status"] == "failed"
    assert jobs["CASE-B"]["status"] == "failed"
    assert jobs["CASE-C"]["status"] == "running"


def test_set_dedup_running_records_started_at(app_row, db_session):
    from app.services.user_settings_service import set_dedup_running

    set_dedup_running("CASE-X", db_session, total=42)
    db_session.commit()
    db_session.refresh(app_row)

    job = app_row.settings_json["dedup_jobs"]["CASE-X"]
    assert job["status"] == "running"
    assert job["started_at"] is not None
    assert job["ended_at"] is None


# ── ai worker concurrency (global AppSettings) ───────────────────────────────


@pytest.mark.unit
def test_get_worker_concurrency_default_when_unset(app_row, db_session):
    from app.services.user_settings_service import (
        DEFAULT_AI_CONCURRENCY,
        get_worker_concurrency,
    )

    assert get_worker_concurrency(db_session) == DEFAULT_AI_CONCURRENCY


@pytest.mark.unit
def test_set_get_worker_concurrency_roundtrip(app_row, db_session):
    from app.services.user_settings_service import (
        get_worker_concurrency,
        set_worker_concurrency,
    )

    set_worker_concurrency(db_session, 8)
    db_session.refresh(app_row)
    assert get_worker_concurrency(db_session) == 8
    assert app_row.settings_json["workers"]["ai_concurrency"] == 8


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0, 17, -1, 2.5, "x", None])
def test_set_worker_concurrency_rejects_out_of_bounds(app_row, db_session, bad):
    from app.services.user_settings_service import set_worker_concurrency

    with pytest.raises(ValueError):
        set_worker_concurrency(db_session, bad)


@pytest.mark.unit
def test_get_worker_concurrency_clamps_invalid_stored(app_row, db_session):
    """A corrupt/out-of-range stored value falls back to the default."""
    from app.services.user_settings_service import (
        DEFAULT_AI_CONCURRENCY,
        get_worker_concurrency,
    )

    app_row.settings_json = {"workers": {"ai_concurrency": 99}}
    db_session.commit()
    assert get_worker_concurrency(db_session) == DEFAULT_AI_CONCURRENCY


@pytest.mark.unit
def test_set_worker_concurrency_records_audit(app_row, db_session):
    from app.models.database import AuditLog
    from app.models.enums import AuditEventType
    from app.services.user_settings_service import set_worker_concurrency

    set_worker_concurrency(db_session, 4)
    log = (
        db_session.query(AuditLog)
        .filter_by(event_type=AuditEventType.SETTINGS_WORKERS_CHANGED)
        .first()
    )
    assert log is not None


# ── ocr worker concurrency (global AppSettings) ──────────────────────────────


@pytest.mark.unit
def test_get_ocr_concurrency_default_when_unset(app_row, db_session):
    from app.services.user_settings_service import (
        DEFAULT_OCR_CONCURRENCY,
        get_ocr_concurrency,
    )

    assert get_ocr_concurrency(db_session) == DEFAULT_OCR_CONCURRENCY


@pytest.mark.unit
def test_set_get_ocr_concurrency_roundtrip(app_row, db_session):
    from app.services.user_settings_service import (
        get_ocr_concurrency,
        set_ocr_concurrency,
    )

    set_ocr_concurrency(db_session, 6)
    db_session.refresh(app_row)
    assert get_ocr_concurrency(db_session) == 6
    assert app_row.settings_json["workers"]["ocr_concurrency"] == 6


@pytest.mark.unit
def test_ocr_and_ai_concurrency_are_independent(app_row, db_session):
    """Setting one worker's concurrency must not disturb the other's key."""
    from app.services.user_settings_service import (
        get_worker_concurrency,
        set_ocr_concurrency,
        set_worker_concurrency,
    )

    set_worker_concurrency(db_session, 5)
    set_ocr_concurrency(db_session, 7)
    db_session.refresh(app_row)
    assert get_worker_concurrency(db_session) == 5
    assert app_row.settings_json["workers"]["ai_concurrency"] == 5
    assert app_row.settings_json["workers"]["ocr_concurrency"] == 7


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0, 17, -1, 2.5, "x", None])
def test_set_ocr_concurrency_rejects_out_of_bounds(app_row, db_session, bad):
    from app.services.user_settings_service import set_ocr_concurrency

    with pytest.raises(ValueError):
        set_ocr_concurrency(db_session, bad)


@pytest.mark.unit
def test_get_ocr_concurrency_clamps_invalid_stored(app_row, db_session):
    """A corrupt/out-of-range stored value falls back to the default."""
    from app.services.user_settings_service import (
        DEFAULT_OCR_CONCURRENCY,
        get_ocr_concurrency,
    )

    app_row.settings_json = {"workers": {"ocr_concurrency": 99}}
    db_session.commit()
    assert get_ocr_concurrency(db_session) == DEFAULT_OCR_CONCURRENCY


@pytest.mark.unit
def test_set_ocr_concurrency_records_audit(app_row, db_session):
    from app.models.database import AuditLog
    from app.models.enums import AuditEventType
    from app.services.user_settings_service import set_ocr_concurrency

    set_ocr_concurrency(db_session, 4)
    log = (
        db_session.query(AuditLog)
        .filter_by(event_type=AuditEventType.SETTINGS_WORKERS_CHANGED)
        .first()
    )
    assert log is not None

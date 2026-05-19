"""Unit tests for user_settings_service."""

from datetime import datetime

import pytest

from app.models.database import Case, Document, UserSettings
from app.models.enums import CaseStatus, Jurisdiction, OriginatorType
from app.services.user_settings_service import (
    count_new_since,
    get_last_viewed,
    mark_viewed,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def settings_row(db_session) -> UserSettings:
    """A UserSettings row with an empty settings_json."""
    row = UserSettings(
        user_id="single_user",
        settings_json={},
    )
    db_session.add(row)
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


# ── get_last_viewed ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_get_last_viewed_no_settings_row(db_session):
    """Returns None when no UserSettings row exists."""
    result = get_last_viewed("SOME-CASE", db_session)
    assert result is None


@pytest.mark.unit
def test_get_last_viewed_no_last_viewed_key(db_session, settings_row):
    """Returns None when settings_json has no last_viewed_cases entry for this case."""
    result = get_last_viewed("NONEXISTENT-CASE", db_session)
    assert result is None


@pytest.mark.unit
def test_get_last_viewed_after_mark_viewed(db_session, settings_row, case_a):
    """After mark_viewed, get_last_viewed returns a datetime close to the given time."""
    fixed_now = datetime(2026, 4, 19, 10, 0, 0)  # naive UTC
    mark_viewed(case_a.id, db_session, now=fixed_now)

    result = get_last_viewed(case_a.id, db_session)
    assert result is not None
    # Compare ISO roundtrip — fromisoformat(isoformat(dt)) is identity
    assert result == fixed_now


@pytest.mark.unit
def test_mark_viewed_no_op_when_no_settings_row(db_session, case_a):
    """mark_viewed is a silent no-op when no UserSettings row exists."""
    # Should not raise
    mark_viewed(case_a.id, db_session)
    # And get_last_viewed still returns None
    assert get_last_viewed(case_a.id, db_session) is None


# ── mark_viewed — multiple cases don't collide ────────────────────────────────


@pytest.mark.unit
def test_multiple_cases_coexist(db_session, settings_row, case_a, case_b):
    """Two cases can be tracked in last_viewed_cases without collision."""
    time_a = datetime(2026, 4, 1, 8, 0, 0)  # naive UTC
    time_b = datetime(2026, 4, 15, 12, 0, 0)  # naive UTC

    mark_viewed(case_a.id, db_session, now=time_a)
    mark_viewed(case_b.id, db_session, now=time_b)

    assert get_last_viewed(case_a.id, db_session) == time_a
    assert get_last_viewed(case_b.id, db_session) == time_b


@pytest.mark.unit
def test_mark_viewed_overwrites_previous(db_session, settings_row, case_a):
    """Calling mark_viewed again updates the stored time."""
    first = datetime(2026, 3, 1, 0, 0, 0)  # naive UTC
    second = datetime(2026, 4, 19, 9, 0, 0)  # naive UTC

    mark_viewed(case_a.id, db_session, now=first)
    mark_viewed(case_a.id, db_session, now=second)

    assert get_last_viewed(case_a.id, db_session) == second


# ── count_new_since ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_count_new_since_none_returns_zero(db_session, case_a):
    """count_new_since returns 0 when since is None."""
    result = count_new_since(case_a.id, None, db_session)
    assert result == 0


@pytest.mark.unit
def test_count_new_since_excludes_old_documents(db_session, case_a, settings_row):
    """Only documents created strictly after `since` are counted via real round-trip."""
    mark_viewed(case_a.id, db_session, now=datetime(2026, 4, 10, 12, 0, 0))

    old_doc = Document(
        title="Old Doc",
        case_id=case_a.id,
        originator_type=OriginatorType.COURT,
        ingest_date=datetime(2026, 4, 9, 0, 0, 0),  # before since
    )
    new_doc = Document(
        title="New Doc",
        case_id=case_a.id,
        originator_type=OriginatorType.COURT,
        ingest_date=datetime(2026, 4, 11, 0, 0, 0),  # after since
    )
    db_session.add_all([old_doc, new_doc])
    db_session.commit()

    since = get_last_viewed(case_a.id, db_session)
    result = count_new_since(case_a.id, since, db_session)
    assert result == 1


@pytest.mark.unit
def test_count_new_since_counts_only_this_case(db_session, case_a, case_b):
    """Documents belonging to other cases are not counted."""
    since = datetime(2026, 4, 1, 0, 0, 0)

    doc_a = Document(
        title="Doc for A",
        case_id=case_a.id,
        originator_type=OriginatorType.OWN,
        ingest_date=datetime(2026, 4, 5, 0, 0, 0),
    )
    doc_b = Document(
        title="Doc for B",
        case_id=case_b.id,
        originator_type=OriginatorType.OWN,
        ingest_date=datetime(2026, 4, 5, 0, 0, 0),
    )
    db_session.add_all([doc_a, doc_b])
    db_session.commit()

    result = count_new_since(case_a.id, since, db_session)
    assert result == 1


@pytest.mark.unit
def test_count_new_since_zero_when_no_new_docs(db_session, case_a):
    """Returns 0 when all documents predate `since`."""
    since = datetime(2026, 4, 20, 0, 0, 0)

    doc = Document(
        title="Old Doc",
        case_id=case_a.id,
        originator_type=OriginatorType.COURT,
        ingest_date=datetime(2026, 4, 1, 0, 0, 0),
    )
    db_session.add(doc)
    db_session.commit()

    result = count_new_since(case_a.id, since, db_session)
    assert result == 0


# ── Stale-job recovery ────────────────────────────────────────────────────────


def _stale_iso(minutes_ago: int) -> str:
    """ISO timestamp `minutes_ago` minutes before now (naive UTC)."""
    from datetime import timedelta

    from app.core.timezone import naive_utc_now

    return (naive_utc_now() - timedelta(minutes=minutes_ago)).isoformat()


def test_recover_stale_reindex_job_flips_old_running(settings_row, db_session):
    """A reindex_job stuck >60min should flip to failed with a 'stale' error."""
    from app.services.user_settings_service import recover_stale_reindex_job

    settings_row.settings_json = {
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
    db_session.refresh(settings_row)
    job = settings_row.settings_json["reindex_job"]
    assert job["status"] == "failed"
    assert "stale" in (job.get("error") or "")
    assert job["ended_at"] is not None


def test_recover_stale_reindex_job_skips_fresh_running(settings_row, db_session):
    """A reindex_job running for only 5min should stay running."""
    from app.services.user_settings_service import recover_stale_reindex_job

    settings_row.settings_json = {
        "reindex_job": {
            "status": "running",
            "started_at": _stale_iso(5),
            "ended_at": None,
        }
    }
    db_session.commit()

    flipped = recover_stale_reindex_job(db_session)

    assert flipped is False
    db_session.refresh(settings_row)
    assert settings_row.settings_json["reindex_job"]["status"] == "running"


def test_recover_stale_reindex_job_skips_done(settings_row, db_session):
    """A completed reindex_job is not re-flipped, even if old."""
    from app.services.user_settings_service import recover_stale_reindex_job

    settings_row.settings_json = {
        "reindex_job": {
            "status": "done",
            "started_at": _stale_iso(120),
            "ended_at": _stale_iso(110),
        }
    }
    db_session.commit()

    flipped = recover_stale_reindex_job(db_session)

    assert flipped is False
    db_session.refresh(settings_row)
    assert settings_row.settings_json["reindex_job"]["status"] == "done"


def test_recover_stale_reindex_job_handles_missing_started_at(settings_row, db_session):
    """A running job without started_at (pre-migration data) is left alone."""
    from app.services.user_settings_service import recover_stale_reindex_job

    settings_row.settings_json = {
        "reindex_job": {"status": "running"}  # no started_at
    }
    db_session.commit()

    flipped = recover_stale_reindex_job(db_session)

    assert flipped is False
    db_session.refresh(settings_row)
    assert settings_row.settings_json["reindex_job"]["status"] == "running"


def test_recover_stale_dedup_jobs_flips_multiple_cases(settings_row, db_session):
    """Multiple stuck dedup_jobs all flip; the case_id list is returned."""
    from app.services.user_settings_service import recover_stale_dedup_jobs

    settings_row.settings_json = {
        "dedup_jobs": {
            "CASE-A": {
                "status": "running",
                "total": 50,
                "processed": 2,
                "started_at": _stale_iso(90),
                "ended_at": None,
            },
            "CASE-B": {
                "status": "running",
                "total": 30,
                "processed": 0,
                "started_at": _stale_iso(120),
                "ended_at": None,
            },
            "CASE-C": {
                "status": "running",
                "total": 10,
                "processed": 5,
                "started_at": _stale_iso(5),  # fresh — should NOT flip
                "ended_at": None,
            },
        }
    }
    db_session.commit()

    flipped = recover_stale_dedup_jobs(db_session)
    db_session.commit()

    assert set(flipped) == {"CASE-A", "CASE-B"}
    db_session.refresh(settings_row)
    jobs = settings_row.settings_json["dedup_jobs"]
    assert jobs["CASE-A"]["status"] == "failed"
    assert jobs["CASE-B"]["status"] == "failed"
    assert jobs["CASE-C"]["status"] == "running"


def test_set_dedup_running_records_started_at(settings_row, db_session):
    """set_dedup_running anchors a started_at timestamp for staleness checks."""
    from app.services.user_settings_service import set_dedup_running

    set_dedup_running("CASE-X", db_session, total=42)
    db_session.commit()
    db_session.refresh(settings_row)

    job = settings_row.settings_json["dedup_jobs"]["CASE-X"]
    assert job["status"] == "running"
    assert job["started_at"] is not None
    assert job["ended_at"] is None

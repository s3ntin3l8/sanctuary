from datetime import datetime, timedelta
from unittest.mock import MagicMock

from app.models.enums import ProceedingStatus
from app.services.case_service import DORMANCY_DAYS, _compute_dormancy_alert


def make_proceeding(
    status=ProceedingStatus.ACTIVE,
    court_name="AG Berlin",
    az="001 F 1/24",
    started_at=None,
    ingest_date=None,
):
    p = MagicMock()
    p.status = status
    p.court_name = court_name
    p.az_court = az
    p.started_at = started_at
    p.ingest_date = ingest_date
    return p


def make_case(proceedings):
    c = MagicMock()
    c.proceedings = proceedings
    return c


def make_db(last_doc_date):
    """Mock db that returns last_doc_date from func.max query."""
    scalar_result = MagicMock()
    scalar_result.scalar.return_value = last_doc_date
    query_mock = MagicMock()
    query_mock.filter.return_value = scalar_result
    db = MagicMock()
    db.query.return_value = query_mock
    return db


def test_no_proceedings_returns_none():
    case = make_case([])
    db = make_db(None)
    assert _compute_dormancy_alert(case, db) is None


def test_no_active_proceedings_returns_none():
    proc = make_proceeding(status=ProceedingStatus.CLOSED)
    case = make_case([proc])
    db = make_db(None)
    assert _compute_dormancy_alert(case, db) is None


def test_recent_activity_returns_none():
    proc = make_proceeding()
    case = make_case([proc])
    recent = datetime.now() - timedelta(days=10)
    db = make_db(recent)
    assert _compute_dormancy_alert(case, db) is None


def test_silent_120_days_returns_alert():
    proc = make_proceeding(court_name="AG Berlin", az="001 F 1/24")
    case = make_case([proc])
    old = datetime.now() - timedelta(days=120)
    db = make_db(old)
    result = _compute_dormancy_alert(case, db)
    assert result is not None
    assert "120" in result


def test_closed_proceeding_not_counted():
    closed = make_proceeding(status=ProceedingStatus.CLOSED)
    case = make_case([closed])
    old = datetime.now() - timedelta(days=200)
    db = make_db(old)
    assert _compute_dormancy_alert(case, db) is None


def test_exactly_dormancy_threshold_not_triggered():
    """Exactly DORMANCY_DAYS days is NOT past threshold (strictly >)."""
    proc = make_proceeding()
    case = make_case([proc])
    # exactly DORMANCY_DAYS days ago — should NOT trigger
    boundary = datetime.now() - timedelta(days=DORMANCY_DAYS)
    db = make_db(boundary)
    result = _compute_dormancy_alert(case, db)
    assert result is None


def test_one_past_dormancy_threshold_triggers():
    """DORMANCY_DAYS + 1 days should trigger."""
    proc = make_proceeding(court_name="LG Hamburg", az="312 O 100/23")
    case = make_case([proc])
    old = datetime.now() - timedelta(days=DORMANCY_DAYS + 1)
    db = make_db(old)
    result = _compute_dormancy_alert(case, db)
    assert result is not None
    assert "LG Hamburg" in result


def test_fallback_to_started_at_when_no_docs():
    """If scalar returns None, fallback to proc.started_at."""
    proc = make_proceeding(
        court_name="OLG Frankfurt",
        az="5 UF 200/24",
        started_at=datetime.now() - timedelta(days=150),
        ingest_date=datetime.now() - timedelta(days=200),
    )
    case = make_case([proc])
    db = make_db(None)  # no documents
    result = _compute_dormancy_alert(case, db)
    # started_at is 150 days ago — should trigger
    assert result is not None
    assert "OLG Frankfurt" in result


def test_multiple_procs_picks_most_dormant():
    """With two dormant proceedings, alert should reference the most dormant one."""
    proc1 = make_proceeding(court_name="AG Berlin", az="001 F 1/24")
    proc2 = make_proceeding(court_name="LG Hamburg", az="312 O 5/23")
    case = make_case([proc1, proc2])

    # proc2 is more dormant (200 days vs 100 days)
    call_count = 0

    def query_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        scalar_result = MagicMock()
        if call_count == 1:
            scalar_result.scalar.return_value = datetime.now() - timedelta(days=100)
        else:
            scalar_result.scalar.return_value = datetime.now() - timedelta(days=200)
        q = MagicMock()
        q.filter.return_value = scalar_result
        return q

    db = MagicMock()
    db.query.side_effect = query_side_effect

    result = _compute_dormancy_alert(case, db)
    assert result is not None
    # proc2 is most dormant — its name should appear
    assert "LG Hamburg" in result

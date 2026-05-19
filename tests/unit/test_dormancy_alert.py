from datetime import datetime, timedelta
from unittest.mock import MagicMock

from app.models.enums import ProceedingStatus
from app.services.case_service import DORMANCY_DAYS, _compute_dormancy_alert

_NEXT_ID = [0]


def make_proceeding(
    status=ProceedingStatus.ACTIVE,
    court_name="AG Berlin",
    az="001 F 1/24",
    started_at=None,
    ingest_date=None,
):
    _NEXT_ID[0] += 1
    p = MagicMock()
    p.id = _NEXT_ID[0]
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


def make_db(rows_by_proc_id):
    """Mock db that returns (proc_id, last_date) tuples from the GROUP BY query.

    ``rows_by_proc_id`` is a dict {proc.id: last_date | None}. Procs with
    None entries are omitted from the result set (matches the real query,
    which returns no row for procs with zero docs).
    """
    all_rows = [(pid, d) for pid, d in rows_by_proc_id.items() if d is not None]
    result_mock = MagicMock()
    result_mock.all.return_value = all_rows
    group_mock = MagicMock()
    group_mock.group_by.return_value = result_mock
    filter_mock = MagicMock()
    filter_mock.filter.return_value = group_mock
    db = MagicMock()
    db.query.return_value = filter_mock
    return db


def test_no_proceedings_returns_none():
    case = make_case([])
    db = make_db({})
    assert _compute_dormancy_alert(case, db) is None


def test_no_active_proceedings_returns_none():
    proc = make_proceeding(status=ProceedingStatus.CLOSED)
    case = make_case([proc])
    db = make_db({})
    assert _compute_dormancy_alert(case, db) is None


def test_recent_activity_returns_none():
    proc = make_proceeding()
    case = make_case([proc])
    recent = datetime.now() - timedelta(days=10)
    db = make_db({proc.id: recent})
    assert _compute_dormancy_alert(case, db) is None


def test_silent_120_days_returns_alert():
    proc = make_proceeding(court_name="AG Berlin", az="001 F 1/24")
    case = make_case([proc])
    old = datetime.now() - timedelta(days=120)
    db = make_db({proc.id: old})
    result = _compute_dormancy_alert(case, db)
    assert result is not None
    assert "120" in result


def test_closed_proceeding_not_counted():
    closed = make_proceeding(status=ProceedingStatus.CLOSED)
    case = make_case([closed])
    db = make_db({})  # filter excludes closed procs before the query
    assert _compute_dormancy_alert(case, db) is None


def test_exactly_dormancy_threshold_not_triggered():
    """Exactly DORMANCY_DAYS days is NOT past threshold (strictly >)."""
    proc = make_proceeding()
    case = make_case([proc])
    boundary = datetime.now() - timedelta(days=DORMANCY_DAYS)
    db = make_db({proc.id: boundary})
    result = _compute_dormancy_alert(case, db)
    assert result is None


def test_one_past_dormancy_threshold_triggers():
    """DORMANCY_DAYS + 1 days should trigger."""
    proc = make_proceeding(court_name="LG Hamburg", az="312 O 100/23")
    case = make_case([proc])
    old = datetime.now() - timedelta(days=DORMANCY_DAYS + 1)
    db = make_db({proc.id: old})
    result = _compute_dormancy_alert(case, db)
    assert result is not None
    assert "LG Hamburg" in result


def test_fallback_to_started_at_when_no_docs():
    """No row for proc.id → fall back to proc.started_at."""
    proc = make_proceeding(
        court_name="OLG Frankfurt",
        az="5 UF 200/24",
        started_at=datetime.now() - timedelta(days=150),
        ingest_date=datetime.now() - timedelta(days=200),
    )
    case = make_case([proc])
    db = make_db({})  # no rows returned from the GROUP BY query
    result = _compute_dormancy_alert(case, db)
    # started_at is 150 days ago — should trigger
    assert result is not None
    assert "OLG Frankfurt" in result


def test_multiple_procs_picks_most_dormant():
    """With two dormant proceedings, alert should reference the most dormant one."""
    proc1 = make_proceeding(court_name="AG Berlin", az="001 F 1/24")
    proc2 = make_proceeding(court_name="LG Hamburg", az="312 O 5/23")
    case = make_case([proc1, proc2])

    db = make_db(
        {
            proc1.id: datetime.now() - timedelta(days=100),
            proc2.id: datetime.now() - timedelta(days=200),
        }
    )

    result = _compute_dormancy_alert(case, db)
    assert result is not None
    # proc2 is most dormant — its name should appear
    assert "LG Hamburg" in result

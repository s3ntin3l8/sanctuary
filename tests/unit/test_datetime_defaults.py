"""Pin that DateTime column defaults are timezone-aware.

Naive datetime defaults caused the 7 deprecation warnings in baseline test
runs and created comparison inconsistencies with `claim_for_analysis`
(which uses `datetime.now(UTC)`).
"""

import warnings

import pytest

from app.models.database import Case, IngestBatch
from app.models.enums import IngestBatchSourceType


def test_default_callable_returns_tz_aware():
    """The shared `_utcnow` default must produce timezone-aware datetimes.

    SQLite stores datetimes as strings without tz info, so round-trip values
    will always come back naive. What matters is that the *write path* uses
    UTC explicitly — same convention as `claim_for_analysis`.
    """
    from app.models.database import _utcnow

    value = _utcnow()
    assert value.tzinfo is not None


@pytest.mark.unit
def test_no_deprecation_warning_on_datetime_insert(db_session):
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        case = Case(id="DT-TEST-001", title="Test")
        db_session.add(case)
        batch = IngestBatch(source_type=IngestBatchSourceType.MANUAL)
        db_session.add(batch)
        db_session.commit()

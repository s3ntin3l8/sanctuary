from datetime import UTC, datetime

import pytest

from app.core.timezone import (
    compare_dates,
    ensure_tz,
    ensure_utc,
    format_date,
    format_datetime,
    now,
    now_utc,
    parse_datetime,
    to_iso,
)


@pytest.mark.unit
def test_now():
    result = now()
    assert result.tzinfo is not None


@pytest.mark.unit
def test_now_utc():
    result = now_utc()
    assert result.tzinfo == UTC


@pytest.mark.unit
def test_ensure_tz_with_naive():
    dt = datetime(2024, 1, 1, 12, 0, 0)
    result = ensure_tz(dt)
    assert result.tzinfo is not None
    assert result.year == 2024


@pytest.mark.unit
def test_ensure_tz_with_aware():
    dt = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    result = ensure_tz(dt)
    assert result == dt


@pytest.mark.unit
def test_ensure_tz_with_none():
    result = ensure_tz(None)
    assert result is None


@pytest.mark.unit
def test_ensure_utc_with_naive():
    dt = datetime(2024, 1, 1, 12, 0, 0)
    result = ensure_utc(dt)
    assert result.tzinfo == UTC


@pytest.mark.unit
def test_ensure_utc_with_none():
    result = ensure_utc(None)
    assert result is None


@pytest.mark.unit
def test_to_iso():
    dt = datetime(2024, 1, 1, 12, 0, 0)
    result = to_iso(dt)
    assert result is not None
    assert "2024" in result


@pytest.mark.unit
def test_to_iso_with_none():
    result = to_iso(None)
    assert result is None


@pytest.mark.unit
def test_parse_datetime_valid():
    result = parse_datetime("2024-01-01T12:00:00")
    assert result is not None
    assert result.year == 2024


@pytest.mark.unit
def test_parse_datetime_with_z():
    result = parse_datetime("2024-01-01T12:00:00Z")
    assert result is not None
    assert result.tzinfo == UTC


@pytest.mark.unit
def test_parse_datetime_empty():
    result = parse_datetime("")
    assert result is None


@pytest.mark.unit
def test_parse_datetime_invalid():
    result = parse_datetime("not-a-date")
    assert result is None


@pytest.mark.unit
def test_format_date():
    dt = datetime(2024, 1, 15, tzinfo=UTC)
    result = format_date(dt)
    assert "15.01.2024" in result


@pytest.mark.unit
def test_format_date_with_none():
    result = format_date(None)
    assert result is None


@pytest.mark.unit
def test_format_datetime():
    dt = datetime(2024, 1, 15, 14, 30, tzinfo=UTC)
    result = format_datetime(dt)
    assert "15.01.2024" in result
    assert "14:30" in result


@pytest.mark.unit
def test_compare_dates_equal():
    dt1 = datetime(2024, 1, 1, tzinfo=UTC)
    dt2 = datetime(2024, 1, 1, tzinfo=UTC)
    assert compare_dates(dt1, dt2) == 0


@pytest.mark.unit
def test_compare_dates_first_greater():
    dt1 = datetime(2024, 1, 2, tzinfo=UTC)
    dt2 = datetime(2024, 1, 1, tzinfo=UTC)
    assert compare_dates(dt1, dt2) == 1


@pytest.mark.unit
def test_compare_dates_second_greater():
    dt1 = datetime(2024, 1, 1, tzinfo=UTC)
    dt2 = datetime(2024, 1, 2, tzinfo=UTC)
    assert compare_dates(dt1, dt2) == -1


@pytest.mark.unit
def test_compare_dates_with_none_both():
    assert compare_dates(None, None) == 0


@pytest.mark.unit
def test_compare_dates_with_none_first():
    dt = datetime(2024, 1, 1, tzinfo=UTC)
    assert compare_dates(None, dt) == -1


@pytest.mark.unit
def test_compare_dates_with_none_second():
    dt = datetime(2024, 1, 1, tzinfo=UTC)
    assert compare_dates(dt, None) == 1


@pytest.mark.unit
def test_compare_dates_naive_become_aware():
    dt1 = datetime(2024, 1, 1)
    dt2 = datetime(2024, 1, 2)
    assert compare_dates(dt1, dt2) == -1

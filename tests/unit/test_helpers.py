from datetime import UTC, datetime, timedelta

import pytest

from app.helpers import format_eur, format_relative_time


@pytest.mark.unit
def test_format_eur():
    assert format_eur(1234.56) == "€\u00a01.234,56"
    assert format_eur(0) == "€\u00a00,00"
    assert format_eur(None) == "—"


@pytest.mark.unit
def test_format_relative_time_just_now():
    now = datetime.now(UTC)
    assert format_relative_time(now) == "just now"


@pytest.mark.unit
def test_format_relative_time_minutes():
    past = datetime.now(UTC) - timedelta(minutes=5)
    assert format_relative_time(past) == "5m ago"


@pytest.mark.unit
def test_format_relative_time_hours():
    past = datetime.now(UTC) - timedelta(hours=2)
    assert format_relative_time(past) == "2h ago"


@pytest.mark.unit
def test_format_relative_time_yesterday():
    past = datetime.now(UTC) - timedelta(days=1)
    # This might depend on the exact second, but for a 1-day offset it should be 'yesterday'
    assert format_relative_time(past) == "yesterday"

"""Unit tests for OriginGuard's _same_origin helper.

The guard is the only line of defense against same-browser cross-origin
mutations for this no-auth single-user app.
"""

from unittest.mock import MagicMock

import pytest

from app.main import _same_origin


def _request(headers: dict, scheme: str = "http") -> MagicMock:
    """Build a FastAPI-Request shaped mock with case-insensitive headers."""
    req = MagicMock()
    # Real FastAPI headers are CIMultiDict; lower-case lookups always work.
    req.headers = {k.lower(): v for k, v in headers.items()}
    req.url.scheme = scheme
    return req


@pytest.mark.unit
def test_same_origin_matches_origin_header():
    """Origin header that equals scheme://host is same-origin."""
    req = _request({"origin": "http://localhost:8000", "host": "localhost:8000"})
    assert _same_origin(req) is True


@pytest.mark.unit
def test_same_origin_rejects_cross_origin_header():
    """Origin header from a different host is cross-origin."""
    req = _request({"origin": "http://evil.example", "host": "localhost:8000"})
    assert _same_origin(req) is False


@pytest.mark.unit
def test_same_origin_matches_https_origin_behind_proxy_seeing_http():
    """TLS-terminating proxy that doesn't forward X-Forwarded-Proto: the app
    sees scheme=http while the browser Origin is https. Same host → allow.
    Regression: this used to 403 every mutation behind such a proxy."""
    req = _request(
        {
            "origin": "https://sanctuary.example.de",
            "host": "sanctuary.example.de",
        },
        scheme="http",
    )
    assert _same_origin(req) is True


@pytest.mark.unit
def test_same_origin_rejects_cross_origin_https():
    """Different host over https is still cross-origin even when host-only."""
    req = _request(
        {"origin": "https://evil.example", "host": "sanctuary.example.de"},
        scheme="http",
    )
    assert _same_origin(req) is False


@pytest.mark.unit
def test_same_origin_passes_when_origin_absent_and_no_sec_fetch():
    """Older browsers / curl: missing Origin + missing Sec-Fetch-Site = allow."""
    req = _request({"host": "localhost:8000"})
    assert _same_origin(req) is True


@pytest.mark.unit
def test_same_origin_rejects_cross_site_sec_fetch_when_origin_absent():
    """Modern browsers send Sec-Fetch-Site even when Origin is suppressed.
    cross-site is an explicit cross-origin signal — reject."""
    req = _request({"host": "localhost:8000", "sec-fetch-site": "cross-site"})
    assert _same_origin(req) is False


@pytest.mark.unit
def test_same_origin_rejects_same_site_sec_fetch_when_origin_absent():
    """same-site (different subdomain) is also cross-origin for our purposes."""
    req = _request({"host": "localhost:8000", "sec-fetch-site": "same-site"})
    assert _same_origin(req) is False


@pytest.mark.unit
def test_same_origin_passes_same_origin_sec_fetch():
    """sec-fetch-site=same-origin is the safe case."""
    req = _request({"host": "localhost:8000", "sec-fetch-site": "same-origin"})
    assert _same_origin(req) is True


@pytest.mark.unit
def test_same_origin_passes_none_sec_fetch():
    """sec-fetch-site=none (user typed URL / bookmark) is allowed."""
    req = _request({"host": "localhost:8000", "sec-fetch-site": "none"})
    assert _same_origin(req) is True


@pytest.mark.unit
def test_same_origin_is_case_insensitive_on_sec_fetch_value():
    """Some test harnesses uppercase header values."""
    req = _request({"host": "localhost:8000", "sec-fetch-site": "CROSS-SITE"})
    assert _same_origin(req) is False

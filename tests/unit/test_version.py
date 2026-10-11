import pytest

from app import version

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_cache():
    version.get_build_info.cache_clear()
    yield
    version.get_build_info.cache_clear()


def test_env_vars_win(monkeypatch):
    monkeypatch.setenv("SANCTUARY_VERSION", "v1.2.3")
    monkeypatch.setenv("SANCTUARY_GIT_SHA", "sha-from-env")
    monkeypatch.setenv("SANCTUARY_BUILD_DATE", "2026-10-11T10:00:00Z")
    assert version.get_build_info() == version.BuildInfo(
        "v1.2.3", "sha-from-env", "2026-10-11T10:00:00Z"
    )


def test_falls_back_to_manifest_and_git(monkeypatch):
    for name in ("SANCTUARY_VERSION", "SANCTUARY_GIT_SHA", "SANCTUARY_BUILD_DATE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(version, "_manifest_version", lambda: "0.4.0")
    monkeypatch.setattr(
        version,
        "_git",
        lambda *a: "sha-from-git" if a[0] == "rev-parse" else "2026-10-10",
    )
    assert version.get_build_info() == version.BuildInfo(
        "0.4.0", "sha-from-git", "2026-10-10"
    )


def test_unknown_when_nothing_resolves(monkeypatch):
    for name in ("SANCTUARY_VERSION", "SANCTUARY_GIT_SHA", "SANCTUARY_BUILD_DATE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(version, "_manifest_version", lambda: None)
    monkeypatch.setattr(version, "_git", lambda *a: None)
    assert version.get_build_info() == version.BuildInfo(
        "unknown", "unknown", "unknown"
    )


def test_blank_env_is_ignored(monkeypatch):
    # An unset Docker build arg arrives as an empty string.
    monkeypatch.setenv("SANCTUARY_VERSION", "")
    monkeypatch.setattr(version, "_manifest_version", lambda: "0.0.0")
    assert version.get_build_info().version == "0.0.0"

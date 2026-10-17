"""The default DATABASE_URL follows the POSTGRES_* variables (docker-compose's)."""

import pytest

from app import config

pytestmark = pytest.mark.unit


def test_the_default_url_uses_the_postgres_variables(monkeypatch):
    monkeypatch.setenv("POSTGRES_USER", "svc")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/word")  # pragma: allowlist secret
    monkeypatch.setenv("POSTGRES_DB", "cases")
    assert (
        config._default_database_url()
        == (
            "postgresql+psycopg://svc:p%40ss%2Fword@localhost:5432/cases"  # pragma: allowlist secret
        )
    )


def test_the_default_url_falls_back_to_the_local_dev_database(monkeypatch):
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        monkeypatch.delenv(name, raising=False)
    assert config._default_database_url().endswith("@localhost:5432/sanctuary")


def test_an_explicit_database_url_wins_over_the_postgres_variables(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://a:b@db.example:5432/x")
    monkeypatch.setenv("POSTGRES_PASSWORD", "ignored")
    assert config._database_url().endswith("@db.example:5432/x")
    monkeypatch.delenv("DATABASE_URL")
    assert "ignored" in config._database_url()


def test_a_missing_password_is_warned_about(monkeypatch, caplog):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with caplog.at_level("WARNING", logger="app.config"):
        config._default_database_url()
    assert any("POSTGRES_PASSWORD is not set" in r.getMessage() for r in caplog.records)

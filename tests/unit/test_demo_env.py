"""The demo instance's isolation guard (scripts/demo/env.py)."""

import socket
from pathlib import Path

import pytest
from scripts.demo.env import (
    DEMO_DB_NAME,
    UNREACHABLE_AI,
    UNREACHABLE_REDIS,
    assert_demo_env,
    assert_ports_free,
    demo_env,
    demo_env_problems,
)

DB_PORT = 3986


def _url(host: str = "127.0.0.1", port: int = DB_PORT, db: str = DEMO_DB_NAME) -> str:
    credentials = "sanctuary:pw"  # pragma: allowlist secret
    return f"postgresql+psycopg://{credentials}@{host}:{port}/{db}"


DEMO_URL = _url()


@pytest.fixture
def demo_dir(tmp_path: Path) -> Path:
    return tmp_path / "demo"


def _clean_env(demo_dir: Path, **overrides: str) -> dict[str, str]:
    return {
        **demo_env(demo_dir, parent={"PATH": "/usr/bin"}),
        "HOST": "127.0.0.1",
        "PORT": "3985",
        "DATABASE_URL": DEMO_URL,
        "SESSION_SECRET": "generated",  # pragma: allowlist secret
        "SECRETS_ENCRYPTION_KEY": "generated",  # pragma: allowlist secret
        "REDIS_URL": UNREACHABLE_REDIS,
        "RATE_LIMIT_STORAGE_URI": "memory://",
        "AI_BASE_URL": UNREACHABLE_AI,
        "FRONTEND_DIST": "/repo/frontend/dist",
        **overrides,
    }


def test_demo_env_inherits_only_the_allowlist(demo_dir: Path) -> None:
    parent = {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "DATABASE_URL": "postgresql://dev",
        "POSTGRES_PASSWORD": "real",  # pragma: allowlist secret
        "SECRETS_ENCRYPTION_KEY": "real",  # pragma: allowlist secret
        "REDIS_URL": "redis://localhost:6379/0",
        "GMAIL_CLIENT_SECRET": "real",  # pragma: allowlist secret
    }
    env = demo_env(demo_dir, parent=parent)
    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "C.UTF-8"
    for leaked in (
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "SECRETS_ENCRYPTION_KEY",
        "REDIS_URL",
    ):
        assert leaked not in env
    assert "GMAIL_CLIENT_SECRET" not in env
    assert env["HOME"].startswith(str(demo_dir))


def test_a_clean_demo_env_has_no_problems(demo_dir: Path) -> None:
    env = _clean_env(demo_dir)
    assert demo_env_problems(env, demo_dir, db_port=DB_PORT) == []
    assert_demo_env(env, demo_dir, db_port=DB_PORT)


def test_an_unset_database_url_is_refused(demo_dir: Path) -> None:
    env = _clean_env(demo_dir)
    del env["DATABASE_URL"]
    assert any(
        "DATABASE_URL must be set" in p for p in demo_env_problems(env, demo_dir)
    )


@pytest.mark.parametrize(
    "poison",
    [
        # the dev database, and variants of it
        {"DATABASE_URL": _url("localhost", 5432, "sanctuary")},
        {"DATABASE_URL": _url(port=5432)},
        {"DATABASE_URL": _url(db="sanctuary")},
        {"DATABASE_URL": _url(host="db.example.com")},
        {"DATABASE_URL": _url(port=3999)},
        {"POSTGRES_PASSWORD": "real"},  # pragma: allowlist secret
        {"REDIS_URL": "redis://localhost:6379/0"},
        {"RATE_LIMIT_STORAGE_URI": "redis://localhost:6379/0"},
        {"AI_BASE_URL": "http://127.0.0.1:11434"},
        {"AI_BASE_URL": "https://api.example.com"},
        {"HOST": "0.0.0.0"},
        {"PORT": "8000"},
        {"PORT": "5432"},
        {"HOME": "/home/me"},
        {"TMPDIR": "/tmp"},
        {"SECRET_KEY": "real"},  # pragma: allowlist secret
        {"AI_API_KEY": "sk-real"},  # pragma: allowlist secret
        {"GMAIL_CLIENT_ID": "x"},
        {"OIDC_ISSUER": "https://idp.example.com"},
        {"BOOTSTRAP_ADMIN_PASSWORD": "real"},  # pragma: allowlist secret
        {"SOMETHING_DIR": "/home/me/data"},
    ],
)
def test_assert_demo_env_refuses_a_poisoned_env(
    demo_dir: Path, poison: dict[str, str]
) -> None:
    env = _clean_env(demo_dir, **poison)
    assert demo_env_problems(env, demo_dir, db_port=DB_PORT)
    with pytest.raises(RuntimeError, match="refused"):
        assert_demo_env(env, demo_dir, db_port=DB_PORT)


def test_assert_demo_env_refuses_a_cwd_with_a_dotenv(
    demo_dir: Path, tmp_path: Path
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text(
        "SECRETS_ENCRYPTION_KEY=real\n"
    )  # pragma: allowlist secret
    env = _clean_env(demo_dir)
    assert any(
        ".env" in p for p in demo_env_problems(env, demo_dir, cwd=cwd, db_port=DB_PORT)
    )


def test_assert_ports_free_rejects_a_listening_port() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        with pytest.raises(RuntimeError, match="already in use"):
            assert_ports_free([port])


@pytest.mark.parametrize("port", [5432, 6379, 8000])
def test_assert_ports_free_rejects_the_dev_stack_ports(port: int) -> None:
    with pytest.raises(RuntimeError, match="reserved"):
        assert_ports_free([port])

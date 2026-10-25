"""The demo launcher's pure parts (scripts/demo/instance.py)."""

import os
from pathlib import Path

import pytest
from scripts.demo import instance


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_spa_is_stale_without_a_build(tmp_path: Path) -> None:
    assert instance.spa_is_stale(tmp_path)


def test_spa_is_stale_follows_the_source_mtime(tmp_path: Path) -> None:
    # A subdirectory: the root conftest drops its own files into tmp_path.
    tmp_path = tmp_path / "frontend"
    _touch(tmp_path / "src" / "App.tsx", 1000)
    _touch(tmp_path / "package.json", 1000)
    _touch(tmp_path / "dist" / "index.html", 2000)
    for path in (tmp_path, tmp_path / "src", tmp_path / "dist"):
        os.utime(path, (1500, 1500))
    assert not instance.spa_is_stale(tmp_path)

    _touch(tmp_path / "src" / "App.tsx", 3000)
    assert instance.spa_is_stale(tmp_path)


def test_the_scratch_tree_holds_the_code_but_never_env_or_data(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    instance._copy_tree(tree)
    names = {p.name for p in tree.iterdir()}
    assert {"app", "alembic", "alembic.ini", "scripts"} <= names
    assert names.isdisjoint({".env", "data", "tests", "frontend", ".git", "scratch"})
    assert not list(tree.rglob("__pycache__"))


def test_stop_is_idempotent_and_removes_the_scratch_dir(tmp_path: Path) -> None:
    demo_dir = tmp_path / "demo"
    demo_dir.mkdir()
    demo = instance.Demo(demo_dir=demo_dir)
    demo.stop()
    demo.stop()
    assert not demo_dir.exists()


def test_keep_leaves_the_scratch_dir(tmp_path: Path) -> None:
    demo_dir = tmp_path / "demo"
    demo_dir.mkdir()
    instance.Demo(demo_dir=demo_dir, keep=True).stop()
    assert demo_dir.exists()


def test_wait_for_http_accepts_a_client_error_but_not_a_server_error() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    status = {"code": 404}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status["code"])
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        instance._wait_for_http(url, timeout=5)  # 404: the server answers
        status["code"] = 503  # e.g. frontend/dist missing
        with pytest.raises(RuntimeError, match="timed out"):
            instance._wait_for_http(url, timeout=1)
    finally:
        server.shutdown()


def test_env_flag_accepts_known_spellings_and_rejects_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value, expected in [
        ("1", True),
        ("TRUE", True),
        ("yes", True),
        ("", False),
        ("0", False),
    ]:
        monkeypatch.setenv("DEMO_FLAG", value)
        assert instance._env_flag("DEMO_FLAG") is expected
    monkeypatch.delenv("DEMO_FLAG")
    assert instance._env_flag("DEMO_FLAG") is False
    monkeypatch.setenv("DEMO_FLAG", "on")
    with pytest.raises(SystemExit):
        instance._env_flag("DEMO_FLAG")

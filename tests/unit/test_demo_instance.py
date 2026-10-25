"""The demo launcher's pure parts (scripts/demo/instance.py)."""

import os
from pathlib import Path

from scripts.demo import instance


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_spa_is_stale_without_a_build(tmp_path: Path) -> None:
    assert instance.spa_is_stale(tmp_path)


def test_spa_is_stale_follows_the_source_mtime(tmp_path: Path) -> None:
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

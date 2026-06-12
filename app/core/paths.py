"""Helpers for persisting and resolving document file paths.

`Document.file_path` is stored **relative to `DATA_DIR`** so the database is
portable across hosts (dev box vs. deployment, where `DATA_DIR` differs). These
two helpers are the single place that converts between the absolute paths used
for filesystem I/O and the relative strings written to the column.

`DATA_DIR` is imported inside each function (not at module top) so the test
fixture's `monkeypatch.setattr(app.config, "DATA_DIR", …)` is honored — the same
pattern used in `app/api/documents.py` and `app/models/database.py`.
"""

from pathlib import Path


def to_storage_path(path: str | Path) -> str:
    """Convert a path to the form stored in `Document.file_path`.

    An absolute path under `DATA_DIR` becomes a string relative to `DATA_DIR`.
    Already-relative paths and absolute paths outside `DATA_DIR` are returned
    unchanged (the latter is the caller's responsibility).
    """
    from app.config import DATA_DIR

    p = Path(path)
    if not p.is_absolute():
        return str(p)
    try:
        return str(p.relative_to(DATA_DIR))
    except ValueError:
        return str(p)


def resolve_storage_path(stored: str | Path) -> Path:
    """Resolve a stored `file_path` (relative or absolute) to an absolute `Path`.

    Relative paths are rooted at `DATA_DIR`; absolute paths are returned as-is.
    """
    from app.config import DATA_DIR

    p = Path(stored)
    return p if p.is_absolute() else DATA_DIR / p

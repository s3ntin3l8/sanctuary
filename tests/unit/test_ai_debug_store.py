"""AI call dumps: opt-in, encrypted per call, pruned by age (7 days)."""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from app import config
from app.services import ai_debug_store
from app.services.intelligence import _ai_call
from app.tasks.maintenance import _prune_log_file

pytestmark = pytest.mark.unit


def _block(label: str, ts: datetime, body: str = "BODY") -> str:
    return (
        f"{'═' * 64}\n# call: {label} | stage=enrich | ts={ts.isoformat()} | prompt_v=1\n\n"
        f"{body}\n"
    )


def _call(path, *, capture, text="Klage gegen Mustermann"):
    _ai_call._write_block(
        path,
        debug_label="doc_1_enrich",
        stage="enrich",
        started_at="2026-10-18T10:00:00+02:00",
        model="m",
        provider="ollama",
        kind="doc",
        scope_id="1",
        ingest_batch_id=3,
        duration_ms=10,
        ttfb_ms=1,
        payload={"messages": [{"role": "user", "content": text}]},
        thinking="",
        response="{}",
        error=None,
        capture=capture,
    )


def test_nothing_is_written_while_capture_is_off(tmp_path):
    path = tmp_path / "ai_debug" / "ib-0003" / "doc_1.md"
    _call(path, capture=False)
    assert not path.exists() and not path.parent.exists()


def test_a_captured_call_is_encrypted_on_disk_and_readable_through_the_store(tmp_path):
    path = tmp_path / "ai_debug" / "ib-0003" / "doc_1.md"
    _call(path, capture=True)
    on_disk = path.read_text()
    assert on_disk.startswith("enc:v1:") and "Mustermann" not in on_disk
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert "Klage gegen Mustermann" in ai_debug_store.read_text(path)
    _call(path, capture=True)
    assert len(path.read_text().splitlines()) == 2  # one line per call


def test_a_call_the_key_cannot_open_says_so(tmp_path, monkeypatch):
    path = tmp_path / "doc_1.md"
    _call(path, capture=True)
    monkeypatch.setattr(
        config, "SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    assert ai_debug_store.UNREADABLE in ai_debug_store.read_text(path)


def test_pruning_drops_old_calls_and_keeps_the_rest_encrypted(tmp_path):
    now = datetime.now(UTC)
    path = tmp_path / "doc_1.md"
    ai_debug_store.append_block(path, _block("old", now - timedelta(days=8), "OLD"))
    ai_debug_store.append_block(path, _block("new", now - timedelta(days=1), "NEW"))
    kept, pruned = _prune_log_file(path, now - timedelta(days=7))
    assert (kept, pruned) == (1, 1)
    assert "Mustermann" not in path.read_text() and "NEW" not in path.read_text()
    text = ai_debug_store.read_text(path)
    assert "NEW" in text and "OLD" not in text


def test_pruning_removes_a_file_when_every_call_is_old(tmp_path):
    path = tmp_path / "doc_1.md"
    ai_debug_store.append_block(
        path, _block("old", datetime.now(UTC) - timedelta(days=30))
    )
    assert _prune_log_file(path, datetime.now(UTC) - timedelta(days=7)) == (0, 1)
    assert not path.exists()


def test_plaintext_dumps_from_before_encryption_are_purged(tmp_path):
    root = tmp_path / "ai_debug"
    (root / "ib-0001").mkdir(parents=True)
    plain = root / "ib-0001" / "doc_9.md"
    plain.write_text("# call: doc_9\nVertraulicher Inhalt\n")
    sealed = root / "doc_1.md"
    ai_debug_store.append_block(sealed, _block("keep", datetime.now(UTC)))
    (root / "runs.jsonl").write_text('{"ts": "t"}\n')
    assert ai_debug_store.purge_plaintext(root) == 1
    assert not plain.exists() and sealed.exists()
    assert (root / "runs.jsonl").exists()  # the metadata index is not a dump

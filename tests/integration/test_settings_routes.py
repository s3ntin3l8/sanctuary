"""Settings API (/api/v1/settings/*): maintenance, appearance, identity, Gmail, AI."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from app.models.database import (
    AppSettings,
    AuditLog,
    Case,
    Entity,
    User,
    UserSettings,
)
from app.models.enums import AuditEventType, EntityType

pytestmark = pytest.mark.integration

client = TestClient(app)


def _app_settings(db):
    db.expire_all()
    return db.query(AppSettings).first().settings_json


def _audit(db, event_type):
    db.expire_all()
    return db.query(AuditLog).filter_by(event_type=event_type).first()


# --- Data / maintenance ------------------------------------------------------


def test_data_view_reports_stats(db_session, sample_case):
    body = client.get("/api/v1/settings/data").json()
    assert body["case_count"] == 1
    assert body["db_size_mb"] > 0


def test_maintenance_reset_ai_enrichment(db_session):
    response = client.post("/api/v1/settings/data/reset-enrichment")
    assert response.status_code == 200
    assert response.json()["message"].startswith("Reset")
    assert _audit(db_session, AuditEventType.MAINTENANCE_RESET_AI_ENRICHMENT)


def test_maintenance_clear_all_data(db_session, sample_case):
    """Wipes workspace data and VACUUMs, but preserves the account, its
    settings, and global app/AI config."""
    admin_id = db_session.query(User).filter_by(email="admin@localhost").one().id
    app_settings_id = db_session.query(AppSettings).first().id
    case_id = sample_case.id

    # Enough rows that VACUUM FULL has measurable space to reclaim.
    db_session.bulk_save_objects(
        [
            Entity(
                case_id=case_id, type=EntityType.PERSON, name=f"Test Entity {i}" * 20
            )
            for i in range(3000)
        ]
    )
    db_session.commit()

    def _db_size() -> int:
        return db_session.execute(
            text("SELECT pg_database_size(current_database())")
        ).scalar()

    size_before = _db_size()
    response = client.post("/api/v1/settings/data/clear-all-data")
    assert response.status_code == 200
    assert response.json()["message"].startswith("Cleared")
    assert _db_size() < size_before, "clear-all-data must VACUUM FULL"

    assert _audit(db_session, AuditEventType.MAINTENANCE_CLEAR_ALL_DATA)
    assert db_session.query(Case).filter_by(id=case_id).first() is None
    assert db_session.query(User).filter_by(id=admin_id).first() is not None
    assert (
        db_session.query(UserSettings).filter_by(user_id=admin_id).first() is not None
    )
    assert (
        db_session.query(AppSettings).filter_by(id=app_settings_id).first() is not None
    )


def test_debug_log_view_rejects_escaping_paths(db_session):
    resp = client.get("/api/v1/settings/ai/logs/view", params={"path": "../x.md"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_path"
    missing = client.get("/api/v1/settings/ai/logs/view", params={"path": "nope.md"})
    assert missing.status_code == 404


def _write_runs(tmp_path, monkeypatch, rows):
    import json

    from app.api.v1 import settings_ai
    from app.services import ai_settings_service

    root = tmp_path / "ai_debug"
    root.mkdir()
    (root / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(settings_ai, "AI_DEBUG_ROOT", root)
    monkeypatch.setattr(ai_settings_service, "AI_DEBUG_ROOT", root)
    return root


def test_ai_logs_filter_and_facets(db_session, tmp_path, monkeypatch):
    _write_runs(
        tmp_path,
        monkeypatch,
        [
            {
                "ts": "t1",
                "kind": "doc",
                "scope_id": 1,
                "stage": "enrich",
                "model": "m1",
                "status": "ok",
                "doc_id": 1,
                "batch_id": 5,
                "duration_ms": 2000,
            },
            {
                "ts": "t2",
                "kind": "doc",
                "scope_id": 2,
                "stage": "claims-p1",
                "model": "m1",
                "status": "error",
                "error": "timeout",
                "doc_id": 2,
                "batch_id": 5,
            },
            {
                "ts": "t3",
                "kind": "misc",
                "scope_id": "dedup_9",
                "stage": "dedup_judge_9",
                "model": "m2",
                "status": "ok",
            },
        ],
    )
    body = client.get("/api/v1/settings/ai/logs").json()
    assert [r["ts"] for r in body["rows"]] == ["t3", "t2", "t1"]
    assert body["facets"] == {
        "kind": ["doc", "misc"],
        "stage": ["claims-p1", "dedup_judge_9", "enrich"],
        "model": ["m1", "m2"],
    }
    errors = client.get("/api/v1/settings/ai/logs", params={"status": "error"}).json()
    assert [r["ts"] for r in errors["rows"]] == ["t2"]
    assert errors["rows"][0]["error"] == "timeout"
    # Facets stay whole while filtering.
    assert errors["facets"]["kind"] == ["doc", "misc"]
    by_batch = client.get(
        "/api/v1/settings/ai/logs", params={"batch_id": 5, "model": "m1"}
    ).json()
    assert [r["ts"] for r in by_batch["rows"]] == ["t2", "t1"]
    by_q = client.get("/api/v1/settings/ai/logs", params={"q": "TIME"}).json()
    assert [r["ts"] for r in by_q["rows"]] == ["t2"]
    assert (
        len(client.get("/api/v1/settings/ai/logs", params={"limit": 1}).json()["rows"])
        == 1
    )


def test_ai_log_view_returns_only_the_requested_call(db_session, tmp_path, monkeypatch):
    root = _write_runs(tmp_path, monkeypatch, [])
    rule = "═" * 64
    (root / "ib-0001").mkdir()
    (root / "ib-0001" / "doc_7.md").write_text(
        f"{rule}\n# call: doc_7_a | stage=metadata | ts=2026-10-10T12:00:00+02:00 | prompt_v=1\n\nFIRST\n\n"
        f"{rule}\n# call: doc_7_b | stage=enrich | ts=2026-10-10T12:05:00+02:00 | prompt_v=1\n\nSECOND\n"
    )
    one = client.get(
        "/api/v1/settings/ai/logs/view",
        params={
            "path": "ib-0001/doc_7.md",
            "stage": "enrich",
            "ts": "2026-10-10T12:05:00+02:00",
        },
    ).json()["body"]
    assert "SECOND" in one and "FIRST" not in one
    whole = client.get(
        "/api/v1/settings/ai/logs/view", params={"path": "ib-0001/doc_7.md"}
    ).json()["body"]
    assert "FIRST" in whole and "SECOND" in whole
    # An unmatched call falls back to the whole file rather than showing nothing.
    fallback = client.get(
        "/api/v1/settings/ai/logs/view",
        params={"path": "ib-0001/doc_7.md", "stage": "nope", "ts": "x"},
    ).json()["body"]
    assert "FIRST" in fallback


def test_data_routes_require_admin(auth_enabled, db_session):
    from app.services import auth_service

    auth_service.create_user(
        db_session,
        email="u@example.com",
        password="password123",  # pragma: allowlist secret
    )
    db_session.commit()
    c = TestClient(app)
    c.post(
        "/api/v1/auth/login",
        json={
            "email": "u@example.com",
            "password": "password123",  # pragma: allowlist secret
        },  # pragma: allowlist secret
    )
    for path in (
        "/api/v1/settings/data",
        "/api/v1/settings/ai",
        "/api/v1/settings/identity",
    ):
        assert c.get(path).status_code == 403, path
    assert c.post("/api/v1/settings/data/clear-all-data").status_code == 403
    assert c.get("/api/v1/settings/data/export").status_code == 403


# --- Appearance --------------------------------------------------------------


def test_appearance_save_timezone_valid(db_session):
    response = client.put(
        "/api/v1/settings/appearance/timezone", json={"tz": "Europe/Berlin"}
    )
    assert response.status_code == 200
    assert response.json()["timezone"] == "Europe/Berlin"
    assert _app_settings(db_session).get("timezone") == "Europe/Berlin"


def test_appearance_save_timezone_invalid():
    response = client.put(
        "/api/v1/settings/appearance/timezone", json={"tz": "Not/AReal/Timezone"}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_timezone"


def test_appearance_save_theme(db_session):
    response = client.put("/api/v1/settings/appearance/theme", json={"theme": "light"})
    assert response.status_code == 200
    assert response.json()["theme"] == "light"
    assert (
        client.put(
            "/api/v1/settings/appearance/theme", json={"theme": "neon"}
        ).status_code
        == 422
    )


def test_appearance_save_dashboard_cards(db_session):
    response = client.put(
        "/api/v1/settings/appearance/dashboard-cards",
        json={"action_items": True, "costs": False, "documents": True},
    )
    assert response.status_code == 200
    assert response.json()["dashboard_cards"] == {
        "action_items": True,
        "costs": False,
        "documents": True,
    }


# --- Identity & context ------------------------------------------------------


def test_identity_roundtrip(db_session):
    body = {
        "own_self": "Dr. Katharina Vogt",
        "own_parties": ["Kanzlei Vogt & Partner", " Vogt Rechtsanwälte ", ""],
        "user_context": "Family law practice in Hamburg.",
    }
    response = client.put("/api/v1/settings/identity", json=body)
    assert response.status_code == 200
    assert response.json() == {
        "own_self": "Dr. Katharina Vogt",
        "own_parties": ["Kanzlei Vogt & Partner", "Vogt Rechtsanwälte"],
        "user_context": "Family law practice in Hamburg.",
    }
    assert client.get("/api/v1/settings/identity").json() == response.json()
    assert _audit(db_session, AuditEventType.SETTINGS_PARTIES_CHANGED)
    assert _audit(db_session, AuditEventType.AI_USER_CONTEXT_CHANGED)


def test_identity_accepts_empty_fields(db_session):
    response = client.put(
        "/api/v1/settings/identity",
        json={"own_self": "", "own_parties": [], "user_context": ""},
    )
    assert response.status_code == 200


# --- Gmail -------------------------------------------------------------------


def test_gmail_filters_roundtrip(db_session):
    response = client.put(
        "/api/v1/settings/gmail/filters",
        json={
            "allowlist": ["a@example.com", " b@example.com ", ""],
            "label_filter": " Sanctuary ",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["allowlist"] == ["a@example.com", "b@example.com"]
    assert body["label_filter"] == "Sanctuary"
    assert body["connected"] is False
    assert body["oauth_start_url"] == "/api/ingest/gmail/oauth/start"
    assert _audit(db_session, AuditEventType.SETTINGS_INGESTION_CHANGED)


# --- AI & models -------------------------------------------------------------


def _create_instance(label="Box", base_url="http://127.0.0.1:11434"):
    resp = client.post(
        "/api/v1/settings/ai/instances", json={"label": label, "base_url": base_url}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_ai_settings_view_has_no_secrets(db_session):
    inst = _create_instance()
    assert inst["has_api_key"] is False
    client.put(
        f"/api/v1/settings/ai/instances/{inst['id']}",
        json={
            "label": "Box",
            "base_url": "http://127.0.0.1:11434",
            "api_key": "sk-secret",  # pragma: allowlist secret
        },  # pragma: allowlist secret
    )
    body = client.get("/api/v1/settings/ai").json()
    assert "sk-secret" not in client.get("/api/v1/settings/ai").text
    (saved,) = body["instances"]
    assert saved["has_api_key"] is True
    assert saved["is_external"] is False
    assert [r["role"] for r in body["roles"]] == ["chat", "embed", "ocr"]
    assert body["roles"][0]["active_id"] == inst["id"]  # first instance is the fallback
    assert body["extraction_engine"] in ("chandra", "docling")
    assert body["reindex_job"] is None


def test_ai_instance_update_keeps_key_when_omitted(db_session):
    inst = _create_instance()
    client.put(
        f"/api/v1/settings/ai/instances/{inst['id']}",
        json={
            "label": "Box",
            "base_url": "http://x",
            "api_key": "k1",  # pragma: allowlist secret
        },  # pragma: allowlist secret
    )
    client.put(
        f"/api/v1/settings/ai/instances/{inst['id']}",
        json={"label": "Renamed", "base_url": "http://x/"},
    )
    saved = next(
        i for i in _app_settings(db_session)["ai"]["instances"] if i["id"] == inst["id"]
    )
    assert saved["label"] == "Renamed"
    assert saved["base_url"] == "http://x"
    # Stored encrypted, never as plaintext; still resolves to the same key.
    assert saved["api_key"].startswith("enc:v1:")
    assert "k1" not in saved["api_key"]
    from app.services.ai_config import get_instance

    stored_key = get_instance(db_session, inst["id"])["api_key"]
    assert stored_key == "k1"  # pragma: allowlist secret


def test_ai_delete_instance(db_session):
    inst = _create_instance()
    other = _create_instance("Other")
    client.put("/api/v1/settings/ai/roles/chat", json={"instance_id": inst["id"]})
    blocked = client.delete(f"/api/v1/settings/ai/instances/{inst['id']}")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "instance_active"
    assert (
        client.delete(f"/api/v1/settings/ai/instances/{other['id']}").status_code == 204
    )
    assert client.delete("/api/v1/settings/ai/instances/inst_nope").status_code == 404


def test_rebuild_index_ddl_failure_is_generic(db_session, monkeypatch):
    """The raw DDL/DB exception must not leak into the response."""
    from sqlalchemy.orm import Session as SASession

    original_execute = SASession.execute

    def _flaky_execute(self, statement, *args, **kwargs):
        if "ALTER TABLE" in str(statement):
            raise RuntimeError("db connection string: postgres://secret@host/db")
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(SASession, "execute", _flaky_execute)
    response = client.post("/api/v1/settings/ai/rebuild-index")
    assert response.status_code == 500
    assert response.json()["code"] == "index_resize_failed"
    assert "postgres://secret" not in response.text


def test_rebuild_index_also_resizes_claims_embedding_column(
    db_session, test_engine, monkeypatch
):
    from sqlalchemy import event

    monkeypatch.setattr("app.tasks.dispatch.dispatch_task", lambda *a, **k: None)
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(test_engine, "before_cursor_execute", _capture)
    try:
        response = client.post("/api/v1/settings/ai/rebuild-index")
    finally:
        event.remove(test_engine, "before_cursor_execute", _capture)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "running"
    assert any("ALTER TABLE claims" in s for s in statements), statements
    assert any("UPDATE claims SET embedding" in s for s in statements), statements
    assert (
        client.get("/api/v1/settings/ai/reindex/status").json()["status"] == "running"
    )
    assert client.post("/api/v1/settings/ai/reindex").status_code == 409


def test_ai_set_role_validation(db_session):
    assert (
        client.put(
            "/api/v1/settings/ai/roles/invalid", json={"instance_id": "x"}
        ).status_code
        == 422
    )
    missing = client.put(
        "/api/v1/settings/ai/roles/chat", json={"instance_id": "inst_nope"}
    )
    assert missing.status_code == 404


def test_ai_set_role_embed_persists_model_on_probe_failure(db_session):
    """A chosen embed model is saved even when the dim probe fails."""
    inst = _create_instance("EmbedBox")
    fake_provider = MagicMock()
    fake_provider.probe_health = AsyncMock(return_value={"ok": False, "detail": "down"})
    fake_provider.get_type = AsyncMock(return_value="ollama")
    with (
        patch(
            "app.api.v1.settings_ai.probe_embed_dim",
            AsyncMock(return_value=(None, "unreachable")),
        ),
        patch("app.api.v1.settings_ai.provider_for", return_value=fake_provider),
    ):
        resp = client.put(
            "/api/v1/settings/ai/roles/embed",
            json={"instance_id": inst["id"], "model": "nomic-embed"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"]["model"] == "nomic-embed"
    assert body["health"] == {"ok": False, "provider": None, "detail": "down"}
    assert "could not be detected" in body["warning"]
    saved = next(
        i for i in _app_settings(db_session)["ai"]["instances"] if i["id"] == inst["id"]
    )
    assert saved["embed_model"] == "nomic-embed"
    assert _app_settings(db_session)["ai"]["active_embed_id"] == inst["id"]


def test_set_worker_concurrency(db_session):
    with patch(
        "app.services.worker_control.apply_ai_concurrency",
        return_value={"live": False, "nodes": []},
    ):
        response = client.put(
            "/api/v1/settings/ai/worker-concurrency", json={"concurrency": 6}
        )
    assert response.status_code == 200
    assert response.json() == {"concurrency": 6, "applied_live": False}
    assert _app_settings(db_session).get("workers", {}).get("ai_concurrency") == 6
    assert _audit(db_session, AuditEventType.SETTINGS_WORKERS_CHANGED)
    for bad in (99, "abc"):
        assert (
            client.put(
                "/api/v1/settings/ai/worker-concurrency", json={"concurrency": bad}
            ).status_code
            == 422
        )


def test_set_ocr_concurrency(db_session):
    with (
        patch(
            "app.services.worker_control.apply_ocr_concurrency",
            return_value={"live": True, "nodes": ["ingest@x"]},
        ),
        patch("app.services.ocr_slots.set_limit") as set_limit,
    ):
        response = client.put(
            "/api/v1/settings/ai/ocr-concurrency", json={"concurrency": 5}
        )
    assert response.status_code == 200
    assert response.json() == {"concurrency": 5, "applied_live": True}
    set_limit.assert_called_once_with(5)
    assert _app_settings(db_session).get("workers", {}).get("ocr_concurrency") == 5


def test_extraction_engine_and_debug_redact(db_session):
    assert client.put(
        "/api/v1/settings/ai/extraction-engine", json={"engine": "docling"}
    ).json() == {"engine": "docling"}
    assert (
        client.put(
            "/api/v1/settings/ai/extraction-engine", json={"engine": "ocrmypdf"}
        ).status_code
        == 422
    )
    assert client.put(
        "/api/v1/settings/ai/debug-redact", json={"enabled": False}
    ).json() == {"enabled": False}
    assert client.get("/api/v1/settings/ai/logs").json()["ai_debug_redact"] is False


# --- Pages -------------------------------------------------------------------


def test_settings_pages_are_spa_routes():
    c = TestClient(app, follow_redirects=False)
    assert c.get("/settings").headers["location"] == "/settings/account"
    for tab in ("account", "gmail", "identity", "ai", "appearance", "data", "export"):
        assert c.get(f"/settings/{tab}").status_code == 200, tab
    assert c.get("/settings/nope").headers["location"] == "/settings/account"
    assert c.get("/admin/users").status_code == 200


def test_regular_user_keeps_personal_settings_but_not_timezone(
    auth_enabled, db_session
):
    from app.services import auth_service

    auth_service.create_user(
        db_session,
        email="u@example.com",
        password="password123",  # pragma: allowlist secret
    )
    db_session.commit()
    c = TestClient(app, follow_redirects=False)
    c.post(
        "/api/v1/auth/login",
        json={
            "email": "u@example.com",
            "password": "password123",  # pragma: allowlist secret
        },  # pragma: allowlist secret
    )
    assert (
        c.put("/api/v1/settings/appearance/theme", json={"theme": "light"}).status_code
        == 200
    )
    assert (
        c.put(
            "/api/v1/settings/gmail/filters", json={"allowlist": ["a@firm.de"]}
        ).status_code
        == 200
    )
    denied = c.put("/api/v1/settings/appearance/timezone", json={"tz": "UTC"})
    assert denied.status_code == 403
    assert c.get("/settings/ai").status_code == 403  # SPA page is gated too
    assert c.get("/settings/account").status_code == 200


def test_ai_instance_update_clears_key_with_empty_string(db_session):
    inst = _create_instance()
    client.put(
        f"/api/v1/settings/ai/instances/{inst['id']}",
        json={
            "label": "Box",
            "base_url": "http://x",
            "api_key": "k1",  # pragma: allowlist secret
        },  # pragma: allowlist secret
    )
    cleared = client.put(
        f"/api/v1/settings/ai/instances/{inst['id']}",
        json={"label": "Box", "base_url": "http://x", "api_key": ""},
    )
    assert cleared.json()["has_api_key"] is False
    saved = next(
        i for i in _app_settings(db_session)["ai"]["instances"] if i["id"] == inst["id"]
    )
    assert saved["api_key"] == "not-needed"  # pragma: allowlist secret

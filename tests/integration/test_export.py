"""Integration tests for the GDPR export endpoint."""

import io
import json
import zipfile
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.rate_limit import limiter
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset the in-memory rate limiter before each test."""
    limiter.reset()
    yield


@pytest.mark.integration
def test_export_returns_zip(db_session):
    response = client.get("/api/v1/settings/data/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "sanctuary_export_" in response.headers["content-disposition"]
    # Verify it's a valid zip
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    names = zf.namelist()
    assert "manifest.json" in names
    assert "README.md" in names
    # At least the user_settings table should be exported
    assert any(n.startswith("data/") for n in names)


@pytest.mark.integration
def test_export_manifest_has_table_counts(db_session):
    response = client.get("/api/v1/settings/data/export")
    assert response.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    manifest = json.loads(zf.read("manifest.json"))
    assert "table_counts" in manifest
    assert "export_date" in manifest
    assert isinstance(manifest["table_counts"], dict)


# --- per-user export (GDPR Art. 15/20 subject access) --------------------------

PASSWORD = "password123"  # pragma: allowlist secret


def _login(email: str) -> TestClient:
    c = TestClient(app, follow_redirects=False)
    c.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    return c


def _rows(zf: zipfile.ZipFile, table: str) -> list[dict]:
    body = zf.read(f"data/{table}.jsonl").decode()
    return [json.loads(line) for line in body.splitlines() if line]


@pytest.mark.integration
def test_user_export_contains_only_what_the_caller_owns(
    auth_enabled, db_session, tmp_path, monkeypatch
):
    from pathlib import Path

    from app.models.database import (
        ActionItem,
        Case,
        CaseShare,
        Document,
        LegalCost,
        UserReaction,
    )
    from app.models.enums import (
        ActionItemType,
        CaseAccessLevel,
        CaseStatus,
        CostCategory,
        CostStatus,
        Jurisdiction,
        UserReactionType,
    )
    from app.services import auth_service, export_service
    from app.services.user_settings_service import set_gmail_credentials

    data_dir = tmp_path / "data"
    monkeypatch.setattr(export_service, "DATA_DIR", data_dir)

    a = auth_service.create_user(db_session, email="a@example.com", password=PASSWORD)
    b = auth_service.create_user(db_session, email="b@example.com", password=PASSWORD)
    db_session.commit()
    for u in (a, b):
        set_gmail_credentials(
            db_session,
            u.id,
            credentials_json=json.dumps({"refresh_token": f"SECRET-{u.id}"}),
            connected_at="2026-01-01T00:00:00Z",
        )
    db_session.commit()

    def case(case_id, owner):
        c = Case(
            id=case_id,
            title=case_id,
            status=CaseStatus.INTAKE,
            jurisdiction=Jurisdiction.DE,
            owner_id=owner.id,
        )
        db_session.add(c)
        return c

    case("X-A", a)
    case("X-B", b)
    case("X-S", b)  # shared with a as viewer
    db_session.add(
        CaseShare(case_id="X-S", user_id=a.id, permission=CaseAccessLevel.VIEWER)
    )
    db_session.flush()

    (data_dir / "X-A").mkdir(parents=True)
    (data_dir / "X-A" / "letter.pdf").write_bytes(b"%PDF a")
    (data_dir / "X-B").mkdir()
    (data_dir / "X-B" / "letter.pdf").write_bytes(b"%PDF b")
    (data_dir / "_TRIAGE").mkdir()
    (data_dir / "_TRIAGE" / "scan.pdf").write_bytes(b"%PDF t")

    doc_a = Document(title="A letter", case_id="X-A", file_path="X-A/letter.pdf")
    doc_b = Document(title="B letter", case_id="X-B", file_path="X-B/letter.pdf")
    doc_s = Document(title="Shared letter", case_id="X-S")
    triage_a = Document(
        title="A scan", case_id=None, owner_id=a.id, file_path="_TRIAGE/scan.pdf"
    )
    triage_b = Document(title="B scan", case_id=None, owner_id=b.id)
    db_session.add_all([doc_a, doc_b, doc_s, triage_a, triage_b])
    db_session.flush()
    db_session.add_all(
        [
            ActionItem(
                case_id="X-A",
                title="A Frist",
                due_date=datetime(2026, 1, 1, tzinfo=UTC),
                action_type=ActionItemType.DEADLINE,
            ),
            ActionItem(
                case_id="X-S",
                title="S Frist",
                due_date=datetime(2026, 1, 1, tzinfo=UTC),
                action_type=ActionItemType.DEADLINE,
            ),
            LegalCost(
                case_id="X-B",
                category=CostCategory.ANWALTSKOSTEN,
                status=CostStatus.OFFEN,
                title="B cost",
                amount_net=1.0,
                amount_gross=1.19,
            ),
            UserReaction(
                document_id=doc_s.id, user_id=a.id, reaction=UserReactionType.TRUE
            ),
            UserReaction(
                document_id=doc_b.id, user_id=b.id, reaction=UserReactionType.LIES
            ),
        ]
    )
    db_session.commit()

    response = _login("a@example.com").get("/api/v1/settings/account/export")
    assert response.status_code == 200
    assert "sanctuary_my_data_" in response.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(response.content))
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["scope"] == "user"

    assert [u["email"] for u in _rows(zf, "users")] == ["a@example.com"]
    assert "password_hash" not in _rows(zf, "users")[0]
    settings = _rows(zf, "user_settings")
    assert len(settings) == 1
    assert settings[0]["settings_json"]["gmail_credentials_json"] == "[redacted]"
    assert "SECRET" not in zf.read("data/user_settings.jsonl").decode()

    assert [c["id"] for c in _rows(zf, "cases")] == ["X-A"]
    assert sorted(d["title"] for d in _rows(zf, "documents")) == ["A letter", "A scan"]
    assert [i["title"] for i in _rows(zf, "action_items")] == ["A Frist"]
    assert _rows(zf, "legal_costs") == []
    # Reactions are the user's own, even on a document they merely view.
    assert [r["document_id"] for r in _rows(zf, "user_reactions")] == [doc_s.id]
    # The share row records what was granted to the user, not the shared case.
    assert [s["case_id"] for s in _rows(zf, "case_shares")] == ["X-S"]

    files = sorted(n for n in zf.namelist() if n.startswith("files/"))
    assert files == ["files/X-A/letter.pdf", "files/_TRIAGE/scan.pdf"]
    assert manifest["files_included"] == 2
    assert Path(data_dir / "X-B" / "letter.pdf").exists()  # untouched, just not shipped


@pytest.mark.integration
def test_user_export_is_open_to_regular_users_and_rate_limited(
    auth_enabled, db_session
):
    from app.services import auth_service

    auth_service.create_user(db_session, email="u@example.com", password=PASSWORD)
    db_session.commit()
    c = _login("u@example.com")
    assert c.get("/api/v1/settings/data/export").status_code == 403
    assert c.get("/api/v1/settings/account/export").status_code == 200

"""Opt-in verifier (#512): a second model checks the local AI's output.

The endpoint is a mocked Anthropic API: every httpx transport is replaced, so a
test that expects no egress can assert that not a single request was made.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.database import (
    AiReviewFinding,
    Case,
    Claim,
    ClaimEvidence,
    Document,
    User,
)
from app.models.enums import (
    CaseStatus,
    ClaimEvidenceRole,
    ClaimStatus,
    ClaimType,
    Jurisdiction,
    OriginatorType,
    PipelineStage,
    ReviewerKind,
    ReviewFindingStatus,
    ReviewTargetKind,
    ReviewVerdict,
    SignificanceTier,
)
from app.services import ai_config
from app.services.ai_config import VerifierSettings
from app.services.ai_provider import chat_provider
from app.services.intelligence import verifier
from app.services.pipeline_status import mark_completed

pytestmark = pytest.mark.integration

client = TestClient(app)

SECRET = "sk-ant-verifier-test-0123456789"  # pragma: allowlist secret
BASE = "https://api.anthropic.com"
MODEL = "claude-test-verifier"


class FakeAnthropic:
    """Answers httpx requests like the Anthropic API and records every one."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.findings: list[dict] = []
        self.raw: str | None = None
        self.status = 200

    def messages(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/v1/messages"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"type": "model", "id": MODEL}]})
        if request.url.path == "/v1/messages":
            if self.status != 200:
                return httpx.Response(
                    self.status,
                    json={"type": "error", "error": {"type": "authentication_error"}},
                )
            text = (
                self.raw
                if self.raw is not None
                else json.dumps({"findings": self.findings})
            )
            events = [
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 120, "output_tokens": 1}},
                },
                {"type": "content_block_delta", "delta": {"text": text}},
                {"type": "message_delta", "usage": {"output_tokens": 42}},
                {"type": "message_stop"},
            ]
            body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
            return httpx.Response(200, content=body.encode())
        return httpx.Response(404)


@pytest.fixture
def fake_api():
    fake = FakeAnthropic()

    def handle(_transport, request):
        return fake.handle(request)

    async def handle_async(_transport, request):
        return fake.handle(request)

    with (
        patch.object(httpx.HTTPTransport, "handle_request", handle),
        patch.object(httpx.AsyncHTTPTransport, "handle_async_request", handle_async),
    ):
        yield fake


def _owner(db) -> User:
    return db.query(User).filter_by(email="admin@localhost").one()


def _configure(
    db, *, enabled=True, case_ids=("TEST-001",), all_cases=False, groups=None
):
    ai_config.save_instance(
        db,
        {
            "id": "inst_cloud",
            "label": "Anthropic",
            "base_url": BASE,
            "provider": "auto",
            "api_key": SECRET,
        },
    )
    ai_config.save_verifier_settings(
        db,
        VerifierSettings(
            enabled=enabled,
            instance_id="inst_cloud",
            model=MODEL,
            groups=tuple(groups or ai_config.VERIFIER_GROUPS),
            all_cases=all_cases,
            case_ids=tuple(case_ids),
        ),
    )


def _case(db, case_id: str) -> Case:
    case = Case(
        id=case_id,
        title=f"Case {case_id}",
        status=CaseStatus.INTAKE,
        jurisdiction=Jurisdiction.DE,
    )
    db.add(case)
    db.commit()
    return case


def _doc(db, case_id: str) -> Document:
    doc = Document(
        title="Beschluss",
        case_id=case_id,
        owner_id=_owner(db).id,
        originator_type=OriginatorType.COURT,
        sender="Amtsgericht Berlin",
        issued_date=datetime(2026, 3, 1, tzinfo=UTC),
        significance_tier=SignificanceTier.INFORMATIONAL,
        content="Der Antrag wird zurückgewiesen. Frist zur Stellungnahme: 2 Wochen.",
        ai_summary={
            "legal_significance": "Der Antrag wird zurückgewiesen.",
            "required_action": "Keine.",
            "financial_impact": "Keine.",
        },
        ai_summary_created_at=datetime(2026, 3, 2, tzinfo=UTC),
    )
    db.add(doc)
    db.commit()
    return doc


def _claim(db, doc: Document) -> Claim:
    claim = Claim(
        claim_text="Der Antrag wurde zurückgewiesen.",
        claim_type=ClaimType.PROCEDURAL,
        status=ClaimStatus.ASSERTED,
    )
    db.add(claim)
    db.flush()
    db.add(
        ClaimEvidence(
            claim_id=claim.id, document_id=doc.id, role=ClaimEvidenceRole.ASSERTS
        )
    )
    db.commit()
    return claim


def _run_claims(db, doc: Document) -> None:
    """CLAIMS for ``doc`` as the pipeline runs it, the queued verification inline."""
    from app.tasks.extract_claims import extract_claims_task
    from app.tasks.verify import verify_document_task

    mark_completed(doc.id, PipelineStage.ENRICH, db)
    with (
        patch("app.services.intelligence.claim_extractor.extract", return_value=None),
        patch("app.services.intelligence.orchestrator.trigger_case_brief_if_ready"),
        patch(
            "app.tasks.verify.verify_document_task.delay",
            side_effect=lambda doc_id: verify_document_task(doc_id),
        ),
    ):
        extract_claims_task(doc.id)


def _findings(db) -> list[AiReviewFinding]:
    db.expire_all()
    return db.query(AiReviewFinding).order_by(AiReviewFinding.id).all()


def _runs(tmp_data) -> list[dict]:
    path = tmp_data / "ai_debug" / "runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# --- No egress unless switched on and in scope ------------------------------------


def test_off_by_default_makes_no_call(db_session, sample_case, fake_api):
    doc = _doc(db_session, sample_case.id)
    _run_claims(db_session, doc)
    assert fake_api.requests == []
    assert _findings(db_session) == []
    assert ai_config.get_verifier_settings(db_session).enabled is False


def test_switched_off_makes_no_call(db_session, sample_case, fake_api):
    _configure(db_session, enabled=False)
    doc = _doc(db_session, sample_case.id)
    _run_claims(db_session, doc)
    assert verifier.verify_document(db_session, doc.id).called is False
    assert fake_api.requests == []


def test_case_not_on_allowlist_makes_no_call(db_session, sample_case, fake_api):
    _configure(db_session, case_ids=("TEST-001",))
    other = _case(db_session, "OTHER-002")
    doc = _doc(db_session, other.id)
    _run_claims(db_session, doc)
    assert verifier.verify_document(db_session, doc.id).called is False
    assert fake_api.requests == []
    assert _findings(db_session) == []


def test_triage_document_makes_no_call_even_for_all_cases(db_session, fake_api):
    _configure(db_session, all_cases=True, case_ids=())
    doc = _doc(db_session, "_TRIAGE")
    assert verifier.verify_document(db_session, doc.id).called is False
    assert fake_api.requests == []


def test_unselected_groups_make_no_call(db_session, sample_case, fake_api):
    _configure(db_session, groups=("brief",))
    doc = _doc(db_session, sample_case.id)
    _run_claims(db_session, doc)
    assert fake_api.requests == []


# --- An allowlisted case ----------------------------------------------------------


def test_claims_in_allowlisted_case_records_verifier_findings(
    db_session, sample_case, fake_api, isolate_data_dir
):
    _configure(db_session)
    doc = _doc(db_session, sample_case.id)
    claim = _claim(db_session, doc)
    fake_api.findings = [
        {
            "target_kind": "document_field",
            "target_id": None,
            "field": "significance_tier",
            "verdict": "disagree",
            "proposed_value": "significant",
            "rationale": "A rejection with a deadline matters.",
            "cited_excerpt": "Der Antrag wird zurückgewiesen.",
        },
        {
            "target_kind": "claim",
            "target_id": claim.id,
            "field": "text",
            "verdict": "agree",
            "proposed_value": None,
            "rationale": "The decision says so.",
            "cited_excerpt": None,
        },
    ]
    chat_url, chat_key = chat_provider.base_url, chat_provider.api_key
    gate = MagicMock(side_effect=AssertionError("verifier took the model gate"))
    with patch("app.services.intelligence._ai_call.model_gate", gate):
        _run_claims(db_session, doc)

    findings = _findings(db_session)
    assert {(f.target_kind, f.field) for f in findings} == {
        (ReviewTargetKind.DOCUMENT_FIELD, "significance_tier"),
        (ReviewTargetKind.CLAIM, "text"),
    }
    assert all(f.reviewer_kind == ReviewerKind.VERIFIER for f in findings)
    assert all(f.agent_token_id is None for f in findings)
    tier = next(f for f in findings if f.field == "significance_tier")
    assert tier.status == ReviewFindingStatus.PENDING
    assert tier.proposed_value == "significant"

    # The request went to the verifier's endpoint, with its model and key.
    (call,) = fake_api.messages()
    body = json.loads(call.content)
    assert body["model"] == MODEL
    assert call.headers["x-api-key"] == SECRET
    assert "Der Antrag wird zurückgewiesen" in body["messages"][0]["content"]

    run = [r for r in _runs(isolate_data_dir) if r["stage"] == "verifier"][-1]
    assert run["model"] == MODEL
    assert run["provider"] == "anthropic"
    assert run["external"] is True
    assert run["doc_id"] == doc.id and run["case_id"] == sample_case.id

    # The chat singleton was not reloaded or repointed; no model gate taken.
    assert (chat_provider.base_url, chat_provider.api_key) == (chat_url, chat_key)
    gate.assert_not_called()


def test_offered_items_only_and_bad_values_dropped(db_session, sample_case, fake_api):
    _configure(db_session)
    doc = _doc(db_session, sample_case.id)
    elsewhere = _doc(db_session, sample_case.id)
    foreign_claim = _claim(db_session, elsewhere)  # never shown for ``doc``
    fake_api.findings = [
        {
            "target_kind": "claim",
            "target_id": foreign_claim.id,
            "field": "status",
            "verdict": "disagree",
            "proposed_value": "refuted",
            "rationale": "Made up.",
            "cited_excerpt": None,
        },
        {
            "target_kind": "document_field",
            "target_id": None,
            "field": "document_type",
            "verdict": "disagree",
            "proposed_value": "not-a-type",
            "rationale": "Bad value.",
            "cited_excerpt": None,
        },
        {
            "target_kind": "summary",
            "target_id": None,
            "field": "required_action",
            "verdict": "disagree",
            "proposed_value": "Stellungnahme binnen 2 Wochen.",
            "rationale": "The order sets a deadline.",
            "cited_excerpt": "Frist zur Stellungnahme: 2 Wochen.",
        },
    ]
    result = verifier.verify_document(db_session, doc.id)
    assert result.called and result.recorded == 1 and result.dropped == 2
    (finding,) = _findings(db_session)
    assert finding.target_kind == ReviewTargetKind.SUMMARY
    assert finding.proposed_value == {
        "legal_significance": "Der Antrag wird zurückgewiesen.",
        "required_action": "Stellungnahme binnen 2 Wochen.",
        "financial_impact": "Keine.",
    }


def test_unusable_answer_records_nothing(db_session, sample_case, fake_api):
    _configure(db_session)
    doc = _doc(db_session, sample_case.id)
    fake_api.raw = "I cannot help with that."
    result = verifier.verify_document(db_session, doc.id)
    assert result.called and result.recorded == 0
    assert _findings(db_session) == []


def test_repeated_agreement_is_one_row(db_session, sample_case, fake_api):
    _configure(db_session, groups=("document_fields",))
    doc = _doc(db_session, sample_case.id)
    fake_api.findings = [
        {
            "target_kind": "document_field",
            "target_id": None,
            "field": "sender",
            "verdict": "agree",
            "proposed_value": None,
            "rationale": "Letterhead.",
            "cited_excerpt": None,
        }
    ]
    verifier.verify_document(db_session, doc.id)
    verifier.verify_document(db_session, doc.id)
    (finding,) = _findings(db_session)
    assert finding.verdict == ReviewVerdict.AGREE
    assert finding.status == ReviewFindingStatus.NOTED


def test_case_brief_is_verified(db_session, sample_case, fake_api):
    _configure(db_session, groups=("brief",))
    sample_case.ai_brief = {"posture": "Defensive.", "next_move": "Wait."}
    db_session.commit()
    fake_api.findings = [
        {
            "target_kind": "case_brief",
            "target_id": None,
            "field": "next_move",
            "verdict": "disagree",
            "proposed_value": "Reply within two weeks.",
            "rationale": "The court set a deadline.",
            "cited_excerpt": None,
        }
    ]
    result = verifier.verify_case_brief(db_session, sample_case.id)
    assert result.recorded == 1
    (finding,) = _findings(db_session)
    assert finding.target_kind == ReviewTargetKind.CASE_BRIEF
    assert finding.document_id is None and finding.case_id == sample_case.id


# --- The API key stays secret -----------------------------------------------------


def test_api_key_never_returned_or_logged(
    db_session, sample_case, fake_api, isolate_data_dir, caplog
):
    _configure(db_session)
    doc = _doc(db_session, sample_case.id)
    fake_api.status = 401
    with caplog.at_level(logging.DEBUG):
        result = verifier.verify_document(db_session, doc.id)
    assert result.called and result.recorded == 0
    assert SECRET not in caplog.text
    assert SECRET not in (isolate_data_dir / "ai_debug" / "runs.jsonl").read_text()
    for url in ("/api/v1/settings/ai/verifier", "/api/v1/settings/ai"):
        response = client.get(url)
        assert response.status_code == 200
        assert SECRET not in response.text


# --- Renaming keeps the allowlist ------------------------------------------------


def test_renaming_an_allowlisted_case_keeps_it_allowlisted(db_session, sample_case):
    from app.services.merge_service import rename_case

    _configure(db_session, case_ids=("TEST-001",))
    rename_case(db_session, "TEST-001", "TEST-009")
    db_session.expire_all()
    assert ai_config.get_verifier_settings(db_session).case_ids == ("TEST-009",)
    assert ai_config.verifier_for_case(db_session, "TEST-009") is not None
    assert ai_config.verifier_for_case(db_session, "TEST-001") is None


# --- Settings API -----------------------------------------------------------------


def test_settings_roundtrip_and_validation(db_session, sample_case):
    ai_config.save_instance(
        db_session,
        {"id": "inst_cloud", "label": "Anthropic", "base_url": BASE, "api_key": SECRET},
    )
    view = client.get("/api/v1/settings/ai/verifier").json()
    assert view["enabled"] is False and view["instance_id"] is None

    incomplete = client.put(
        "/api/v1/settings/ai/verifier",
        json={"enabled": True, "instance_id": "inst_cloud", "model": MODEL},
    )
    assert incomplete.status_code == 422
    assert incomplete.json()["code"] == "verifier_incomplete"

    unknown = client.put(
        "/api/v1/settings/ai/verifier",
        json={"enabled": False, "case_ids": ["NOPE-1"]},
    )
    assert unknown.json()["code"] == "unknown_case"

    ok = client.put(
        "/api/v1/settings/ai/verifier",
        json={
            "enabled": True,
            "instance_id": "inst_cloud",
            "model": MODEL,
            "groups": ["claims", "brief", "claims"],
            "case_ids": [" TEST-001 "],
        },
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["groups"] == ["claims", "brief"]
    assert body["case_ids"] == ["TEST-001"]
    assert body["is_external"] is True
    assert body["endpoint_label"] == "Anthropic"
    assert SECRET not in ok.text

    # The endpoint the verifier uses cannot be removed from under it.
    assert client.delete("/api/v1/settings/ai/instances/inst_cloud").status_code == 409


# --- Verify on demand -------------------------------------------------------------


def test_verify_routes_follow_the_scope(db_session, sample_case):
    doc = _doc(db_session, sample_case.id)
    status = client.get(f"/api/v1/documents/{doc.id}/verifier").json()
    assert status == {"available": False, "model": None}
    with patch("app.tasks.dispatch.dispatch_task") as dispatch:
        refused = client.post(f"/api/v1/documents/{doc.id}/verify")
        assert refused.status_code == 409
        assert refused.json()["code"] == "verifier_unavailable"
        dispatch.assert_not_called()

        _configure(db_session)
        assert client.get(f"/api/v1/documents/{doc.id}/verifier").json() == {
            "available": True,
            "model": MODEL,
        }
        assert client.get(f"/api/v1/cases/{sample_case.id}/verifier").json() == {
            "available": True,
            "model": MODEL,
        }
        assert client.post(f"/api/v1/documents/{doc.id}/verify").status_code == 202
        assert client.post(f"/api/v1/cases/{sample_case.id}/verify").status_code == 202
        assert dispatch.call_count == 2


def test_merging_an_allowlisted_case_does_not_widen_the_scope(db_session, sample_case):
    from app.services.merge_service import merge_cases

    _case(db_session, "OTHER-002")
    _configure(db_session, case_ids=("TEST-001",))
    merge_cases(db_session, "TEST-001", "OTHER-002")
    db_session.expire_all()
    assert ai_config.get_verifier_settings(db_session).case_ids == ()
    assert ai_config.verifier_for_case(db_session, "OTHER-002") is None


@pytest.mark.parametrize("method", ["delete_and_revert", "purge"])
def test_deleting_an_allowlisted_case_takes_it_off_the_list(
    db_session, sample_case, method
):
    from app.services.case_service import CaseService

    _configure(db_session, case_ids=("TEST-001",))
    getattr(CaseService(db_session), method)("TEST-001")
    db_session.expire_all()
    assert ai_config.get_verifier_settings(db_session).case_ids == ()


def test_edges_into_another_case_are_not_sent(db_session, sample_case, fake_api):
    from app.models.database import DocumentRelationship
    from app.models.enums import RelationshipConfidence, RelationshipType

    _configure(db_session, groups=("relationships",))
    doc = _doc(db_session, sample_case.id)
    other = _case(db_session, "OTHER-002")
    foreign = _doc(db_session, other.id)
    foreign.title = "Geheimes Gutachten"
    db_session.add(
        DocumentRelationship(
            from_document_id=doc.id,
            to_document_id=foreign.id,
            relationship_type=RelationshipType.REFERENCES,
            confidence=RelationshipConfidence.AI_DETECTED,
        )
    )
    db_session.commit()
    assert verifier.verify_document(db_session, doc.id).called is False
    assert fake_api.requests == []


# --- Debug dumps follow the opt-in capture switch ----------------------------------


def test_prompt_dump_only_with_capture_and_encrypted(
    db_session, sample_case, fake_api, isolate_data_dir
):
    from app.services import ai_debug_store
    from app.services.user_settings_service import set_ai_debug_capture

    _configure(db_session, groups=("document_fields",))
    doc = _doc(db_session, sample_case.id)
    dump = isolate_data_dir / "ai_debug" / "unbatched" / f"doc_{doc.id}.md"
    verifier.verify_document(db_session, doc.id)
    assert not dump.exists()

    set_ai_debug_capture(db_session, True)
    verifier.verify_document(db_session, doc.id)
    raw = dump.read_text()
    assert raw.startswith("enc:v1:")
    assert "zurückgewiesen" not in raw
    plain = ai_debug_store.read_text(dump)
    assert "stage=verifier" in plain and "zurückgewiesen" in plain
    assert SECRET not in plain


# --- Case verification ------------------------------------------------------------


def test_verify_case_task_out_of_scope_does_nothing(db_session, sample_case, fake_api):
    from app.tasks.verify import verify_case_task

    _configure(db_session, case_ids=("OTHER-002",))
    _doc(db_session, sample_case.id)
    with patch("app.tasks.verify.verify_document_task.delay") as delay:
        verify_case_task(sample_case.id)
    delay.assert_not_called()
    assert fake_api.requests == []


def test_verify_case_task_checks_brief_and_queues_each_document(
    db_session, sample_case, fake_api
):
    from app.tasks.verify import verify_case_task

    _configure(db_session)
    sample_case.ai_brief = {"posture": "Defensive."}
    db_session.commit()
    docs = [_doc(db_session, sample_case.id) for _ in range(2)]
    with patch("app.tasks.verify.verify_document_task.delay") as delay:
        result = verify_case_task(sample_case.id)
    assert result["brief_checked"] is True
    assert len(fake_api.messages()) == 1  # the brief; documents run in their own tasks
    assert sorted(c.args[0] for c in delay.call_args_list) == [d.id for d in docs]

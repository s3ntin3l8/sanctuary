"""Tests for app/services/claim_embedding.py — specifically that embedding
failures now record `Claim.embedding_failed_at` so the system has signal."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.config import AI_EMBED_DIM
from app.models.database import Case, Claim
from app.models.enums import CaseStatus, ClaimStatus, ClaimType
from app.services.claim_embedding import upsert_claim_embedding


@pytest.fixture
def claim_in_case(db_session):
    case = Case(id="EMB-001", title="Embed test", status=CaseStatus.INTAKE)
    db_session.add(case)
    db_session.commit()
    c = Claim(
        claim_text="The opposing party did not appear at the hearing on 2025-09-15.",
        claim_type=ClaimType.FACTUAL,
        status=ClaimStatus.ASSERTED,
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


@pytest.mark.unit
def test_upsert_claim_embedding_marks_failure_on_endpoint_error(
    db_session, claim_in_case
):
    """When embed_claim_text returns None (endpoint failure / model unloaded),
    upsert_claim_embedding must set Claim.embedding_failed_at so the system
    has persistent signal that dedup is degraded for this claim."""
    with patch(
        "app.services.claim_embedding.embed_claim_text",
        new=AsyncMock(return_value=None),
    ):
        ok = asyncio.run(upsert_claim_embedding(claim_in_case.id, db_session))

    assert ok is False
    db_session.refresh(claim_in_case)
    assert claim_in_case.embedding_failed_at is not None


@pytest.mark.unit
def test_retry_failed_claim_embeddings_recovers_eligible_claims(
    db_session, claim_in_case
):
    """The maintenance task picks up claims with embedding_failed_at older
    than the cooldown and re-attempts via upsert_claim_embedding. On success
    the timestamp is cleared (idempotency + self-healing)."""
    from datetime import UTC, datetime, timedelta

    from app.tasks.maintenance import (
        _RETRY_COOLDOWN_MINUTES,
        retry_failed_claim_embeddings_task,
    )

    # Pre-mark the claim as failed, with a timestamp safely beyond cooldown.
    claim_in_case.embedding_failed_at = datetime.now(UTC) - timedelta(
        minutes=_RETRY_COOLDOWN_MINUTES + 1
    )
    db_session.commit()
    claim_id = claim_in_case.id

    fake_vec = [0.01] * AI_EMBED_DIM
    with (
        patch(
            "app.services.claim_embedding.embed_claim_text",
            new=AsyncMock(return_value=fake_vec),
        ),
        # The maintenance task opens fresh SessionLocal()'s — point them at
        # the test's db_session so writes land in the same in-memory DB.
        patch("app.config.SessionLocal", return_value=db_session),
        patch.object(db_session, "close", return_value=None),
    ):
        result = retry_failed_claim_embeddings_task()

    assert result["attempted"] == 1
    assert result["recovered"] == 1
    db_session.refresh(claim_in_case)
    assert claim_in_case.embedding_failed_at is None
    # Sanity: claim_id is still the same row, nothing was deleted.
    db_session.expire_all()
    assert db_session.get(Claim, claim_id) is not None


@pytest.mark.unit
def test_retry_failed_claim_embeddings_respects_cooldown(db_session, claim_in_case):
    """A claim that JUST failed (timestamp within cooldown) is not retried
    yet — the in-line attempt that just failed would re-fire against the
    same overloaded backend."""
    from datetime import UTC, datetime, timedelta

    from app.tasks.maintenance import (
        _RETRY_COOLDOWN_MINUTES,
        retry_failed_claim_embeddings_task,
    )

    claim_in_case.embedding_failed_at = datetime.now(UTC) - timedelta(
        minutes=_RETRY_COOLDOWN_MINUTES - 1
    )
    db_session.commit()

    with (
        patch(
            "app.services.claim_embedding.embed_claim_text",
            new=AsyncMock(return_value=[0.01] * AI_EMBED_DIM),
        ) as mock_embed,
        patch("app.config.SessionLocal", return_value=db_session),
        patch.object(db_session, "close", return_value=None),
    ):
        result = retry_failed_claim_embeddings_task()

    assert result["attempted"] == 0
    assert result["recovered"] == 0
    mock_embed.assert_not_called()


@pytest.mark.unit
def test_upsert_claim_embedding_clears_failure_on_success(db_session, claim_in_case):
    """A recovered claim — previously marked failed — must have
    embedding_failed_at cleared back to NULL when the embed succeeds."""
    from datetime import UTC, datetime

    # Pre-set the failure timestamp to simulate a prior failed attempt.
    claim_in_case.embedding_failed_at = datetime(2026, 5, 22, 18, 0, tzinfo=UTC)
    db_session.commit()

    # Must match Claim.embedding's declared pgvector dimension (AI_EMBED_DIM).
    fake_vec = [0.01] * AI_EMBED_DIM

    with patch(
        "app.services.claim_embedding.embed_claim_text",
        new=AsyncMock(return_value=fake_vec),
    ):
        ok = asyncio.run(upsert_claim_embedding(claim_in_case.id, db_session))

    assert ok is True
    db_session.refresh(claim_in_case)
    assert claim_in_case.embedding_failed_at is None

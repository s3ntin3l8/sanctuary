"""Unit tests for Phase 8 CaseGraphService — lane assignment, bundle detection,
significance filtering, and end-to-end payload build.

The lane assignment, filter, and bundle-header helpers are module-level
functions — we exercise them directly with `types.SimpleNamespace` stand-ins so
the tests stay a few microseconds long and require no DB round-trip.

`build_payload` is additionally covered with a real in-memory sqlite DB so
we know the SQL queries and wiring through `DocumentRepository` /
`DocumentRelationshipRepository` work end-to-end.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.database import (
    Document,
    DocumentRelationship,
    Proceeding,
)
from app.models.enums import (
    DocumentRole,
    OriginatorType,
    ProceedingCourtLevel,
    ProceedingStatus,
    RelationshipType,
    SignificanceTier,
)
from app.services.case_graph_service import (
    CaseGraphService,
    _is_bundle_header,
    _lane_for,
    passes_filter,
)

# ---------------------------------------------------------------------------
# Helpers — build minimal "doc-like" namespaces for the pure-function tests.
# ---------------------------------------------------------------------------


def _mk_doc(
    *,
    id: int = 1,
    originator_type: OriginatorType | None = OriginatorType.OWN,
    attributed_originator: str | None = None,
    court_relay: bool = False,
    role: DocumentRole = DocumentRole.STANDALONE,
    significance_tier: SignificanceTier | None = SignificanceTier.SIGNIFICANT,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=id,
        originator_type=originator_type,
        attributed_originator=attributed_originator,
        court_relay=court_relay,
        role=role,
        significance_tier=significance_tier,
    )


# ===========================================================================
# Lane assignment
# ===========================================================================


class TestCaseGraphServiceLaneAssignment:
    @pytest.mark.unit
    def test_own_originator_goes_to_own_lane(self):
        doc = _mk_doc(originator_type=OriginatorType.OWN)
        assert _lane_for(doc) == "own"

    @pytest.mark.unit
    def test_court_originator_goes_to_court_lane(self):
        doc = _mk_doc(originator_type=OriginatorType.COURT, attributed_originator=None)
        assert _lane_for(doc) == "court"

    @pytest.mark.unit
    def test_attributed_originator_overrides_originator_type(self):
        """Critical court-relay case: the true sender is `opposing`, not `court`."""
        doc = _mk_doc(
            originator_type=OriginatorType.COURT,
            attributed_originator="opposing",
        )
        assert _lane_for(doc) == "opposing"

    @pytest.mark.unit
    def test_third_party_attributed_originator(self):
        doc = _mk_doc(
            originator_type=OriginatorType.COURT,
            attributed_originator="third_party",
        )
        assert _lane_for(doc) == "third"

    @pytest.mark.unit
    def test_own_attributed_originator(self):
        doc = _mk_doc(
            originator_type=OriginatorType.COURT,
            attributed_originator="own",
        )
        assert _lane_for(doc) == "own"

    @pytest.mark.unit
    def test_unknown_attributed_originator_falls_back_to_originator_type(self, caplog):
        """A garbage string must not crash — it should fall back to originator_type
        and emit a warning."""
        doc = _mk_doc(
            originator_type=OriginatorType.COURT,
            attributed_originator="garbage_value",
        )
        with caplog.at_level("WARNING"):
            lane = _lane_for(doc)
        assert lane == "court"  # falls back to originator_type
        assert any(
            "Unknown attributed_originator" in rec.message for rec in caplog.records
        )

    @pytest.mark.unit
    def test_missing_originator_type_defaults_to_own(self, caplog):
        doc = _mk_doc(originator_type=None, attributed_originator=None)
        with caplog.at_level("WARNING"):
            lane = _lane_for(doc)
        assert lane == "own"


# ===========================================================================
# Significance filter
# ===========================================================================


class TestCaseGraphServiceSignificanceFilter:
    @pytest.mark.unit
    def test_critical_filter_keeps_only_critical_docs(self):
        critical = _mk_doc(significance_tier=SignificanceTier.CRITICAL)
        significant = _mk_doc(significance_tier=SignificanceTier.SIGNIFICANT)
        informational = _mk_doc(significance_tier=SignificanceTier.INFORMATIONAL)
        administrative = _mk_doc(significance_tier=SignificanceTier.ADMINISTRATIVE)

        assert passes_filter(critical, "critical") is True
        assert passes_filter(significant, "critical") is False
        assert passes_filter(informational, "critical") is False
        assert passes_filter(administrative, "critical") is False

    @pytest.mark.unit
    def test_significant_plus_drops_administrative(self):
        admin = _mk_doc(
            significance_tier=SignificanceTier.ADMINISTRATIVE,
            role=DocumentRole.STANDALONE,  # not a relay
        )
        assert passes_filter(admin, "significant+") is False

    @pytest.mark.unit
    def test_significant_plus_keeps_relay(self):
        """Administrative relay bundles stay visible in `significant+` mode."""
        relay = _mk_doc(
            significance_tier=SignificanceTier.ADMINISTRATIVE,
            role=DocumentRole.COVER_LETTER,
        )
        assert passes_filter(relay, "significant+") is True

    @pytest.mark.unit
    def test_significant_plus_keeps_non_administrative(self):
        for tier in (
            SignificanceTier.CRITICAL,
            SignificanceTier.SIGNIFICANT,
            SignificanceTier.INFORMATIONAL,
        ):
            doc = _mk_doc(significance_tier=tier, role=DocumentRole.STANDALONE)
            assert passes_filter(doc, "significant+") is True, f"tier={tier}"

    @pytest.mark.unit
    def test_all_filter_keeps_everything(self):
        for tier in (
            SignificanceTier.CRITICAL,
            SignificanceTier.SIGNIFICANT,
            SignificanceTier.INFORMATIONAL,
            SignificanceTier.ADMINISTRATIVE,
        ):
            doc = _mk_doc(significance_tier=tier)
            assert passes_filter(doc, "all") is True, f"tier={tier}"


# ===========================================================================
# Bundle detection
# ===========================================================================


class TestCaseGraphServiceBundleDetection:
    @pytest.mark.unit
    def test_bundle_header_detection(self):
        doc = _mk_doc(court_relay=True, role=DocumentRole.COVER_LETTER)
        assert _is_bundle_header(doc) is True

    @pytest.mark.unit
    def test_non_relay_not_bundle(self):
        doc = _mk_doc(court_relay=False, role=DocumentRole.COVER_LETTER)
        assert _is_bundle_header(doc) is False

    @pytest.mark.unit
    def test_relay_but_not_cover_letter_not_bundle(self):
        doc = _mk_doc(court_relay=True, role=DocumentRole.STANDALONE)
        assert _is_bundle_header(doc) is False

    @pytest.mark.unit
    def test_standalone_not_bundle(self):
        doc = _mk_doc(court_relay=False, role=DocumentRole.STANDALONE)
        assert _is_bundle_header(doc) is False


# ===========================================================================
# build_payload integration — real SQLite DB
# ===========================================================================


@pytest.fixture
def proceeding_with_graph(db_session, sample_case) -> Proceeding:
    """A Proceeding with a small mix of documents so build_payload has real data."""
    proceeding = Proceeding(
        case_id=sample_case.id,
        court_name="Amtsgericht Hamburg",
        court_level=ProceedingCourtLevel.AG,
        az_court="003 F 426/25",
        status=ProceedingStatus.ACTIVE,
        ingest_date=datetime(2025, 1, 1),
    )
    db_session.add(proceeding)
    db_session.flush()

    # OWN — a filing we sent
    own_doc = Document(
        title="Unsere Klageerwiderung",
        case_id=sample_case.id,
        proceeding_id=proceeding.id,
        originator_type=OriginatorType.OWN,
        role=DocumentRole.STANDALONE,
        significance_tier=SignificanceTier.SIGNIFICANT,
        received_date=datetime(2025, 1, 10),
    )
    # COURT relay cover letter (bundle header) — delivers opposing's pleading
    cover = Document(
        title="Übersendung Gegenschrift",
        case_id=sample_case.id,
        proceeding_id=proceeding.id,
        originator_type=OriginatorType.COURT,
        court_relay=True,
        role=DocumentRole.COVER_LETTER,
        significance_tier=SignificanceTier.ADMINISTRATIVE,
        received_date=datetime(2025, 2, 1),
    )
    db_session.add_all([own_doc, cover])
    db_session.flush()

    # Enclosure (child of the cover letter, truly authored by opposing)
    enclosure = Document(
        title="Gegenschrift der Gegenseite",
        case_id=sample_case.id,
        proceeding_id=proceeding.id,
        originator_type=OriginatorType.COURT,  # wrapped by court
        attributed_originator="opposing",  # true author
        parent_id=cover.id,
        role=DocumentRole.ENCLOSURE,
        significance_tier=SignificanceTier.SIGNIFICANT,
        received_date=datetime(2025, 2, 1),
    )
    # A low-noise administrative standalone doc (should be hidden in significant+)
    admin = Document(
        title="Empfangsbestätigung",
        case_id=sample_case.id,
        proceeding_id=proceeding.id,
        originator_type=OriginatorType.COURT,
        role=DocumentRole.STANDALONE,
        significance_tier=SignificanceTier.ADMINISTRATIVE,
        received_date=datetime(2025, 2, 15),
    )
    db_session.add_all([enclosure, admin])
    db_session.flush()

    # A REPLIES_TO edge between own_doc and cover
    rel = DocumentRelationship(
        from_document_id=cover.id,
        to_document_id=own_doc.id,
        relationship_type=RelationshipType.REPLIES_TO,
    )
    db_session.add(rel)
    db_session.commit()
    db_session.refresh(proceeding)
    return proceeding


class TestCaseGraphServiceBuildPayload:
    @pytest.mark.unit
    def test_build_payload_on_empty_proceeding(self, db_session, sample_case):
        proc = Proceeding(
            case_id=sample_case.id,
            court_name="AG",
            court_level=ProceedingCourtLevel.AG,
            status=ProceedingStatus.ACTIVE,
            ingest_date=datetime(2025, 1, 1),
        )
        db_session.add(proc)
        db_session.commit()
        db_session.refresh(proc)

        payload = CaseGraphService(db_session).build_payload(proc.id)

        assert payload.nodes == []
        assert payload.edges == []
        assert payload.bundles == []
        assert payload.node_count == 0
        assert payload.edge_count == 0
        assert payload.filter == "significant+"
        # The 4 fixed lanes are always present
        assert [lane["key"] for lane in payload.lanes] == [
            "own",
            "court",
            "opposing",
            "third",
        ]

    @pytest.mark.unit
    def test_build_payload_counts_and_filters(self, db_session, proceeding_with_graph):
        payload = CaseGraphService(db_session).build_payload(
            proceeding_with_graph.id, significance_filter="significant+"
        )

        # Expect 2 visible top-level nodes: own_doc + cover (bundle header).
        # - enclosure is a child → never a standalone node
        # - admin is administrative standalone → filtered out by significant+
        assert payload.node_count == 2
        titles = {n["full_title"] for n in payload.nodes}
        assert "Unsere Klageerwiderung" in titles
        assert "Übersendung Gegenschrift" in titles
        assert "Empfangsbestätigung" not in titles

        # node_counts reflects per-tier breakdown for Alpine hiddenCount()
        assert payload.node_counts["administrative_standalone"] == 1

    @pytest.mark.unit
    def test_build_payload_emits_bundle_for_court_relay(
        self, db_session, proceeding_with_graph
    ):
        payload = CaseGraphService(db_session).build_payload(proceeding_with_graph.id)
        assert len(payload.bundles) == 1
        bundle = payload.bundles[0]
        assert len(bundle["children"]) == 1
        # True-author attribution flows into the bundle child's origin lane
        assert bundle["children"][0]["origin"] == "opposing"

    @pytest.mark.unit
    def test_build_payload_emits_reply_edge(self, db_session, proceeding_with_graph):
        payload = CaseGraphService(db_session).build_payload(proceeding_with_graph.id)
        assert payload.edge_count == 1
        edge = payload.edges[0]
        assert edge["kind"] == "reply"
        assert edge["arrow"] is True

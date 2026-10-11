"""Family context: the case cascades through a family, the proceeding only to
members that print no Aktenzeichen of their own."""

from datetime import UTC, datetime

import pytest

from app.models.database import (
    BatchSubGroup,
    Case,
    Document,
    IngestBatch,
    Proceeding,
    User,
)
from app.models.enums import (
    CaseStatus,
    DocumentRole,
    DocumentType,
    IngestBatchSourceType,
    IngestBatchStatus,
    OriginatorType,
    ProceedingCourtLevel,
    ProceedingStatus,
)
from app.services import family_context as fc

pytestmark = pytest.mark.unit

AZ = "3 F 1221/26"
OTHER_AZ = "2 K 92/25"


@pytest.fixture
def env(db_session):
    owner = db_session.query(User).filter_by(email="admin@localhost").one()
    for case_id in ("FC-1", "FC-2"):
        db_session.add(
            Case(id=case_id, title=case_id, status=CaseStatus.INTAKE, owner_id=owner.id)
        )
    batch = IngestBatch(
        owner_id=owner.id,
        source_type=IngestBatchSourceType.SCAN,
        status=IngestBatchStatus.PROCESSING,
        received_at=datetime(2026, 10, 9, tzinfo=UTC),
    )
    db_session.add(batch)
    db_session.flush()

    def proc(case_id="FC-1", az=AZ, draft=False):
        p = Proceeding(
            case_id=case_id,
            court_name="Amtsgericht Ingolstadt",
            court_level=ProceedingCourtLevel.AG,
            az_court=az,
            status=ProceedingStatus.ACTIVE,
            is_draft=draft,
        )
        db_session.add(p)
        db_session.flush()
        return p

    def doc(
        title,
        *,
        case_id="_TRIAGE",
        parent=None,
        az=None,
        proceeding=None,
        role=None,
        sub_group=None,
        originator=OriginatorType.OPPOSING,
        **kw,
    ):
        d = Document(
            title=title,
            owner_id=owner.id,
            case_id=case_id,
            ingest_batch_id=batch.id,
            parent_id=parent.id if parent else None,
            role=role
            or (DocumentRole.ENCLOSURE if parent else DocumentRole.STANDALONE),
            originator_type=originator,
            az_court=az,
            proceeding_id=proceeding.id if proceeding else None,
            sub_group_id=sub_group.id if sub_group else None,
            **kw,
        )
        db_session.add(d)
        db_session.flush()
        return d

    return db_session, batch, proc, doc


def test_case_source_and_marker(env):
    db, _batch, _proc, doc = env
    d = doc("d", case_id="FC-1")
    assert fc.case_source(d) == "own"
    fc.mark_case(d, "inherited")
    assert fc.case_source(d) == "inherited"
    fc.mark_case(d, "user_set")
    assert fc.case_source(d) == "user"
    fc.mark_case(d, None)
    assert fc.case_source(d) == "own"
    assert fc.case_source(doc("loose")) is None


def test_is_exhibit_depends_on_who_wrote_the_parent(env):
    db, _batch, _proc, doc = env
    motion = doc("motion", role=DocumentRole.COVER_LETTER)
    relay = doc(
        "relay",
        role=DocumentRole.COVER_LETTER,
        originator=OriginatorType.COURT,
        document_type=DocumentType.RELAY,
    )
    exhibit = doc("exhibit", parent=motion)
    relayed = doc("relayed filing", parent=relay)
    assert fc.is_exhibit(exhibit)
    assert not fc.is_exhibit(relayed)
    assert not fc.is_exhibit(motion)


def test_case_cascades_from_the_root_to_its_children(env):
    db, batch, proc, doc = env
    root = doc("motion", case_id="FC-1", role=DocumentRole.COVER_LETTER)
    child = doc("exhibit", parent=root)
    grandchild = doc("page of exhibit", parent=child)

    changed = fc.propagate(db, grandchild)

    assert {d.id for d in changed} == {child.id, grandchild.id}
    for d in (child, grandchild):
        assert d.case_id == "FC-1"
        assert fc.case_source(d) == "inherited"
    assert fc.case_source(root) == "own"
    assert batch.case_id == "FC-1"


def test_a_child_that_resolved_first_pulls_its_root_in(env):
    db, _batch, _proc, doc = env
    root = doc("motion", role=DocumentRole.COVER_LETTER)
    child = doc("exhibit", parent=root, case_id="FC-1")

    fc.propagate(db, child)

    assert root.case_id == "FC-1"
    assert fc.case_source(root) == "inherited"


def test_an_own_case_is_never_overwritten(env):
    db, _batch, _proc, doc = env
    root = doc("motion", case_id="FC-1", role=DocumentRole.COVER_LETTER)
    elsewhere = doc("exhibit", parent=root, case_id="FC-2")
    user_filed = doc("exhibit 2", parent=root, case_id="FC-2")
    fc.mark_case(user_filed, "user_set")

    fc.propagate(db, root)

    assert elsewhere.case_id == "FC-2"
    assert user_filed.case_id == "FC-2"


def test_an_inherited_case_follows_the_family_when_it_moves(env):
    db, _batch, _proc, doc = env
    root = doc("motion", case_id="FC-1", role=DocumentRole.COVER_LETTER)
    child = doc("exhibit", parent=root)
    fc.propagate(db, root)
    assert child.case_id == "FC-1"

    root.case_id = "FC-2"
    fc.propagate(db, root)

    assert child.case_id == "FC-2"


def test_proceeding_goes_only_to_members_without_their_own_az(env):
    db, _batch, proc, doc = env
    live = proc(az=AZ)
    root = doc(
        "motion", case_id="FC-1", az=AZ, proceeding=live, role=DocumentRole.COVER_LETTER
    )
    plain = doc("declaration", parent=root)
    copy_of_ruling = doc("old ruling", parent=root, az=OTHER_AZ)

    fc.propagate(db, root)

    assert plain.proceeding_id == live.id
    assert copy_of_ruling.proceeding_id is None


def test_observed_az_beats_an_inherited_proceeding(env):
    db, _batch, proc, doc = env
    live = proc(az=AZ)
    other = proc(az=OTHER_AZ)
    root = doc(
        "motion", case_id="FC-1", az=AZ, proceeding=live, role=DocumentRole.COVER_LETTER
    )
    # Wrongly given the live proceeding earlier (what the old batch-wide rewrite did).
    exhibit = doc("old ruling", parent=root, az=OTHER_AZ, proceeding=live)

    fc.propagate(db, root)

    assert exhibit.proceeding_id == other.id


def test_a_confirmed_member_keeps_its_proceeding(env):
    db, _batch, proc, doc = env
    live = proc(az=AZ)
    other = proc(az=OTHER_AZ)
    root = doc(
        "motion", case_id="FC-1", az=AZ, proceeding=live, role=DocumentRole.COVER_LETTER
    )
    kept = doc(
        "ruling", parent=root, az=OTHER_AZ, proceeding=live, confirmed_at=datetime.now()
    )
    plain = doc(
        "declaration", parent=root, proceeding=other, confirmed_at=datetime.now()
    )

    fc.propagate(db, root)

    assert kept.proceeding_id == live.id
    assert plain.proceeding_id == other.id


def test_a_roots_proceeding_is_never_reset_by_its_own_stored_az(env):
    db, _batch, proc, doc = env
    new_instance = proc(az="22 T 342/26")
    root = doc(
        "appeal",
        case_id="FC-1",
        az=AZ,  # stale: the proceeding was forked to the new instance
        proceeding=new_instance,
        role=DocumentRole.COVER_LETTER,
    )
    child = doc("annex", parent=root)

    fc.propagate(db, root)

    assert root.proceeding_id == new_instance.id
    assert child.proceeding_id == new_instance.id


def test_a_manual_sub_group_is_one_family_without_parent_links(env):
    db, batch, proc, doc = env
    group = BatchSubGroup(batch_id=batch.id, label=None, sort_order=0)
    db.add(group)
    db.flush()
    a = doc("a", case_id="FC-1", sub_group=group, sub_group_sort_order=0)
    b = doc("b", sub_group=group, sub_group_sort_order=1)
    outsider = doc("outsider")

    fc.propagate(db, a)

    assert b.case_id == "FC-1"
    assert outsider.case_id == "_TRIAGE"


def test_batch_fallback_needs_the_families_to_agree(env):
    db, batch, _proc, doc = env
    first = doc("first letter", case_id="FC-1")
    loose = doc("loose page")

    fc.propagate_batch(db, batch.id)
    assert loose.case_id == "FC-1"
    assert fc.case_source(loose) == "inherited"
    assert batch.case_id == "FC-1"

    # Two letters filed into two cases: nothing is guessed for the rest.
    loose.case_id = "_TRIAGE"
    fc.mark_case(loose, None)
    doc("second letter", case_id="FC-2")
    fc.propagate_batch(db, batch.id)
    assert loose.case_id == "_TRIAGE"
    assert first.case_id == "FC-1"


def test_batch_removes_a_draft_proceeding_only_an_exhibit_seeded(env):
    db, batch, proc, doc = env
    live = proc(az=AZ)
    draft = proc(az=OTHER_AZ, draft=True)
    root = doc(
        "motion", case_id="FC-1", az=AZ, proceeding=live, role=DocumentRole.COVER_LETTER
    )
    exhibit = doc("old ruling", parent=root, az=OTHER_AZ, proceeding=draft)
    batch.proceeding_id = draft.id
    db.flush()

    fc.propagate_batch(db, batch.id)
    db.flush()

    assert exhibit.proceeding_id is None
    assert db.get(Proceeding, draft.id) is None
    assert batch.proceeding_id == live.id


def test_the_sub_group_anchor_may_sit_on_any_descendant(env):
    """The root has no sub-group; a descendant does. Asking about that
    descendant selects its sub-group, siblings included."""
    db, batch, _proc, doc = env
    group = BatchSubGroup(batch_id=batch.id, label=None, sort_order=0)
    db.add(group)
    db.flush()
    root = doc("root", case_id="FC-1")
    child = doc("child", parent=root, sub_group=group, sub_group_sort_order=0)
    sibling = doc("sibling", sub_group=group, sub_group_sort_order=1)

    members = fc.family_members(db, child)

    assert {m.id for m in members} == {child.id, sibling.id}


def test_a_draft_another_bundle_still_points_at_survives_the_exhibit_cleanup(env):
    db, batch, proc, doc = env
    live = proc(az=AZ)
    draft = proc(az=OTHER_AZ, draft=True)
    root = doc(
        "motion", case_id="FC-1", az=AZ, proceeding=live, role=DocumentRole.COVER_LETTER
    )
    doc("old ruling", parent=root, az=OTHER_AZ, proceeding=draft)
    other_batch = IngestBatch(
        owner_id=batch.owner_id,
        source_type=IngestBatchSourceType.SCAN,
        status=IngestBatchStatus.PROCESSING,
        received_at=datetime(2026, 10, 9, tzinfo=UTC),
        proceeding_id=draft.id,
    )
    db.add(other_batch)
    db.flush()

    fc.propagate_batch(db, batch.id)
    db.flush()

    assert db.get(Proceeding, draft.id) is not None

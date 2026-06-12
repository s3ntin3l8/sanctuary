"""Unit tests for the generic BaseRepository CRUD operations."""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import joinedload

from app.models.database import Document
from app.repositories.base import BaseRepository


@pytest.fixture
def repo(db_session):
    return BaseRepository(Document, db_session)


@pytest.mark.unit
def test_create_and_get(repo):
    doc = repo.create(title="Base Repo Doc")
    assert doc.id is not None
    assert repo.get(doc.id).title == "Base Repo Doc"
    # options branch + unknown id
    assert repo.get(doc.id, options=[joinedload(Document.ingest_batch)]).id == doc.id
    assert repo.get(999999) is None


@pytest.mark.unit
def test_get_by_and_get_all(repo):
    repo.create(title="unique-getby-title")
    assert repo.get_by(title="unique-getby-title") is not None
    assert (
        repo.get_by(options=[joinedload(Document.ingest_batch)], title="nope") is None
    )

    repo.create(title="shared-title")
    repo.create(title="shared-title")
    rows = repo.get_all(title="shared-title")
    assert len(rows) == 2
    assert repo.get_all(
        options=[joinedload(Document.ingest_batch)], title="shared-title"
    )


@pytest.mark.unit
def test_get_paginated(repo):
    for i in range(3):
        repo.create(title=f"page-doc-{i}")
    page = repo.get_paginated(offset=0, limit=2, title=None)
    assert isinstance(page, list)
    limited = repo.get_paginated(
        offset=0, limit=1, options=[joinedload(Document.ingest_batch)]
    )
    assert len(limited) <= 1


@pytest.mark.unit
def test_count_and_exists(repo):
    repo.create(title="countable-xyz")
    assert repo.count(title="countable-xyz") == 1
    assert repo.exists(title="countable-xyz") is True
    assert repo.exists(title="does-not-exist-zzz") is False


@pytest.mark.unit
def test_update(repo):
    doc = repo.create(title="before")
    updated = repo.update(doc.id, title="after")
    assert updated.title == "after"
    assert repo.update(999999, title="ghost") is None


@pytest.mark.unit
def test_delete(repo):
    doc = repo.create(title="to-delete")
    assert repo.delete(doc.id) is True
    assert repo.get(doc.id) is None
    assert repo.delete(999999) is False


@pytest.mark.unit
def test_bulk_create_and_execute(repo):
    created = repo.bulk_create([{"title": "bulk-1"}, {"title": "bulk-2"}])
    assert len(created) == 2
    result = repo.execute(text("SELECT 1"))
    assert result.scalar() == 1

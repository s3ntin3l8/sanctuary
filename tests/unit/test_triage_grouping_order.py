from datetime import UTC, datetime

import pytest

from app.models import Document
from app.services.triage_grouping import document_sort_key


@pytest.mark.parametrize("arrival", [(7, 3, 5), (5, 7, 3)])
def test_documents_tied_on_every_part_order_by_id(arrival: tuple[int, ...]) -> None:
    at = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    docs = [Document(id=i, ingest_date=at) for i in arrival]

    assert [d.id for d in sorted(docs, key=document_sort_key)] == [3, 5, 7]

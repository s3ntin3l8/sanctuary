"""A model-gate timeout is not a conversion failure."""

from unittest.mock import patch

import pytest

from app.services.ingestion import service
from app.services.model_gate import ModelGateTimeout


@pytest.mark.unit
def test_gate_timeout_escapes_process_uploaded_document_unwrapped(
    db_session, sample_document, tmp_path
):
    """Wrapped in IngestionError it would fail the document for good; escaping
    as-is lets process_document_task retry once the gate frees up."""
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    sample_document.file_path = str(pdf)
    db_session.commit()

    with (
        patch.object(service, "resolve_storage_path", return_value=pdf),
        patch.object(service, "convert_file", side_effect=ModelGateTimeout("1800s")),
    ):
        with pytest.raises(ModelGateTimeout):
            service.process_uploaded_document(sample_document, db_session)


@pytest.mark.unit
def test_other_conversion_errors_are_still_wrapped(
    db_session, sample_document, tmp_path
):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    sample_document.file_path = str(pdf)
    db_session.commit()

    with (
        patch.object(service, "resolve_storage_path", return_value=pdf),
        patch.object(service, "convert_file", side_effect=RuntimeError("boom")),
    ):
        with pytest.raises(service.IngestionError, match="Conversion error"):
            service.process_uploaded_document(sample_document, db_session)

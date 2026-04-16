import pytest

from app.core.exceptions import (
    AIProcessingError,
    DatabaseError,
    NotFoundError,
    ProcessingError,
    SanctuaryError,
    ValidationError,
    http_exception,
    not_found_exception,
    server_error_exception,
    validation_exception,
)


@pytest.mark.unit
def test_sanctuary_error():
    err = SanctuaryError("Test error", {"key": "value"})
    assert err.message == "Test error"
    assert err.details == {"key": "value"}


@pytest.mark.unit
def test_not_found_error():
    err = NotFoundError("Case", "ADV-001")
    assert "Case" in err.message
    assert "ADV-001" in err.message


@pytest.mark.unit
def test_validation_error():
    err = ValidationError("case_id", "Invalid format")
    assert "case_id" in err.message


@pytest.mark.unit
def test_processing_error():
    err = ProcessingError("Conversion failed", 123)
    assert err.message == "Conversion failed"
    assert err.details["document_id"] == 123


@pytest.mark.unit
def test_database_error():
    err = DatabaseError("insert", Exception("DB error"))
    assert "insert" in err.message


@pytest.mark.unit
def test_ai_processing_error():
    err = AIProcessingError("Model error", "ollama")
    assert err.message == "Model error"
    assert err.details["service"] == "ollama"


@pytest.mark.unit
def test_http_exception_helpers():
    exc = http_exception(400, "Bad request")
    assert exc.status_code == 400
    assert exc.detail == "Bad request"


@pytest.mark.unit
def test_not_found_exception():
    exc = not_found_exception("Case", "ADV-001")
    assert exc.status_code == 404
    assert "ADV-001" in exc.detail


@pytest.mark.unit
def test_validation_exception():
    exc = validation_exception("Invalid input")
    assert exc.status_code == 422
    assert exc.detail == "Invalid input"


@pytest.mark.unit
def test_server_error_exception():
    exc = server_error_exception("Something went wrong")
    assert exc.status_code == 500
    assert exc.detail == "Something went wrong"


@pytest.mark.unit
def test_server_error_default_message():
    exc = server_error_exception()
    assert exc.status_code == 500
    assert exc.detail == "Internal server error"

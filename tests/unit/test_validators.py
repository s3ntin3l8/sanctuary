import pytest

from app.core.validators import (
    validate_case_id,
    validate_case_id_required,
    validate_case_status,
    validate_date,
    validate_email,
    validate_file_extension,
    validate_file_size,
    validate_jurisdiction,
    validate_optional_string,
    validate_positive_float,
    validate_required_string,
    validate_title,
)


@pytest.mark.unit
def test_validate_case_id_valid():
    assert validate_case_id("ADV-123-K") == "ADV-123-K"
    assert validate_case_id("REF-441-22") == "REF-441-22"


@pytest.mark.unit
def test_validate_case_id_triage():
    assert validate_case_id("_TRIAGE") == "_TRIAGE"


@pytest.mark.unit
def test_validate_case_id_invalid():
    assert validate_case_id("invalid") is None
    assert validate_case_id("") is None
    assert validate_case_id(None) is None


@pytest.mark.unit
def test_validate_case_id_required_valid():
    assert validate_case_id_required("ADV-123-K") == "ADV-123-K"


@pytest.mark.unit
def test_validate_case_id_required_invalid():
    with pytest.raises(ValueError):
        validate_case_id_required("invalid")

    with pytest.raises(ValueError):
        validate_case_id_required("_TRIAGE")

    with pytest.raises(ValueError):
        validate_case_id_required("")


@pytest.mark.unit
def test_validate_case_status_valid():
    from app.models.enums import CaseStatus

    assert validate_case_status("intake") == CaseStatus.INTAKE
    assert validate_case_status("DISCOVERY") == CaseStatus.DISCOVERY
    assert validate_case_status("trial") == CaseStatus.TRIAL


@pytest.mark.unit
def test_validate_case_status_invalid():
    assert validate_case_status("invalid") is None
    assert validate_case_status("") is None
    assert validate_case_status(None) is None


@pytest.mark.unit
def test_validate_jurisdiction_valid():
    from app.models.enums import Jurisdiction

    assert validate_jurisdiction("de") == Jurisdiction.DE
    assert validate_jurisdiction("UK") == Jurisdiction.UK
    assert validate_jurisdiction("us") == Jurisdiction.US


@pytest.mark.unit
def test_validate_jurisdiction_invalid():
    assert validate_jurisdiction("invalid") is None
    assert validate_jurisdiction("") is None


@pytest.mark.unit
def test_validate_date_datetime():
    from datetime import datetime

    dt = datetime(2024, 1, 1)
    result = validate_date(dt)
    assert result is not None
    assert result.year == 2024


@pytest.mark.unit
def test_validate_date_string():
    result = validate_date("2024-01-01")
    assert result is not None
    assert result.year == 2024


@pytest.mark.unit
def test_validate_date_string_with_time():
    result = validate_date("2024-01-01T12:00:00")
    assert result is not None
    assert result.hour == 12


@pytest.mark.unit
def test_validate_date_invalid():
    assert validate_date("not-a-date") is None
    assert validate_date(None) is None


@pytest.mark.unit
def test_validate_positive_float_valid():
    assert validate_positive_float(100.0) == 100.0
    assert validate_positive_float("500.50") == 500.50
    assert validate_positive_float(0) == 0


@pytest.mark.unit
def test_validate_positive_float_invalid():
    assert validate_positive_float(-100) is None
    assert validate_positive_float("invalid") is None
    assert validate_positive_float(None) is None


@pytest.mark.unit
def test_validate_file_extension_valid():
    assert validate_file_extension("document.pdf") is True
    assert validate_file_extension("document.docx") is True
    assert validate_file_extension("document.txt") is True


@pytest.mark.unit
def test_validate_file_extension_invalid():
    assert validate_file_extension("document.exe") is False
    assert validate_file_extension("document") is False
    assert validate_file_extension("") is False


@pytest.mark.unit
def test_validate_file_extension_custom_allowed():
    assert validate_file_extension("document.csv", [".csv"]) is True
    assert validate_file_extension("document.pdf", [".csv"]) is False


@pytest.mark.unit
def test_validate_file_size_valid():
    assert validate_file_size(1024) is True
    assert validate_file_size(1024 * 1024) is True
    assert validate_file_size(50 * 1024 * 1024) is True


@pytest.mark.unit
def test_validate_file_size_invalid():
    assert validate_file_size(0) is False
    assert validate_file_size(100 * 1024 * 1024) is False


@pytest.mark.unit
def test_validate_required_string_valid():
    assert validate_required_string("hello", "name") == "hello"
    assert validate_required_string("  hello  ", "name") == "hello"


@pytest.mark.unit
def test_validate_required_string_invalid():
    with pytest.raises(ValueError):
        validate_required_string("", "name")

    with pytest.raises(ValueError):
        validate_required_string("   ", "name")

    with pytest.raises(ValueError):
        validate_required_string(None, "name")


@pytest.mark.unit
def test_validate_optional_string():
    assert validate_optional_string("hello") == "hello"
    assert validate_optional_string("  hello  ") == "hello"
    assert validate_optional_string("") is None
    assert validate_optional_string("   ") is None
    assert validate_optional_string(None) is None


@pytest.mark.unit
def test_validate_title():
    assert validate_title("My Document") == "My Document"
    assert validate_title("  Document Title  ") == "Document Title"
    assert validate_title("") is None
    assert validate_title(None) is None


@pytest.mark.unit
def test_validate_email_valid():
    assert validate_email("test@example.com") == "test@example.com"
    assert validate_email("USER@DOMAIN.COM") == "user@domain.com"


@pytest.mark.unit
def test_validate_email_invalid():
    assert validate_email("invalid") is None
    assert validate_email("@example.com") is None
    assert validate_email("test@") is None
    assert validate_email("") is None
    assert validate_email(None) is None

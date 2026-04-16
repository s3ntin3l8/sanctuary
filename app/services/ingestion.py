"""
Ingestion service - backward compatibility re-export.

All functionality has been moved to app.services.ingestion module.
"""

from app.services.ingestion import (
    IngestionError,
    compute_review_reasons,
    convert_file,
    extract_case_id,
    extract_clean_title,
    extract_cost_candidates,
    extract_legal_categories,
    extract_originator,
    extract_received_date,
    extract_schedule_candidates,
    extract_sender,
    get_allowed_extensions,
    is_allowed_extension,
    is_valid_docling_output,
    parse_eml_file,
    process_uploaded_document,
)

__all__ = [
    "convert_file",
    "get_allowed_extensions",
    "is_allowed_extension",
    "is_valid_docling_output",
    "parse_eml_file",
    "extract_case_id",
    "extract_cost_candidates",
    "extract_legal_categories",
    "extract_originator",
    "extract_received_date",
    "extract_sender",
    "extract_schedule_candidates",
    "IngestionError",
    "compute_review_reasons",
    "extract_clean_title",
    "process_uploaded_document",
]

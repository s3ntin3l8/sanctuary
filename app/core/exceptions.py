from fastapi import HTTPException, status


class SanctuaryError(Exception):
    """Base exception for Sanctuary application."""

    def __init__(self, message: str, details: dict | None = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class NotFoundError(SanctuaryError):
    """Resource not found."""

    def __init__(self, resource: str, identifier: str | int):
        super().__init__(
            f"{resource} not found: {identifier}",
            {"resource": resource, "identifier": identifier},
        )


class ValidationError(SanctuaryError):
    """Input validation failure."""

    def __init__(self, field: str, message: str):
        super().__init__(f"Validation failed for {field}: {message}", {"field": field})


class ProcessingError(SanctuaryError):
    """Document processing failure."""

    def __init__(self, message: str, document_id: int | None = None):
        details = {"document_id": document_id} if document_id else {}
        super().__init__(message, details)


class DatabaseError(SanctuaryError):
    """Database operation failure."""

    def __init__(self, operation: str, original_error: Exception):
        super().__init__(
            f"Database error during {operation}: {str(original_error)}",
            {"operation": operation, "original_error": str(original_error)},
        )


class AIProcessingError(SanctuaryError):
    """AI service failure."""

    def __init__(self, message: str, service: str | None = None):
        details = {"service": service} if service else {}
        super().__init__(message, details)


def http_exception(status_code: int, message: str) -> HTTPException:
    """Create HTTPException with given status and message."""
    return HTTPException(status_code=status_code, detail=message)


def not_found_exception(resource: str, identifier: str | int) -> HTTPException:
    """Create 404 HTTPException."""
    return http_exception(
        status.HTTP_404_NOT_FOUND, f"{resource} not found: {identifier}"
    )


def validation_exception(message: str) -> HTTPException:
    """Create 422 HTTPException for validation errors."""
    return http_exception(422, message)


def server_error_exception(message: str = "Internal server error") -> HTTPException:
    """Create 500 HTTPException."""
    return http_exception(status.HTTP_500_INTERNAL_SERVER_ERROR, message)

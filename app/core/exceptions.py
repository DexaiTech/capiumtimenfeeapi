"""Application exceptions.

Services raise these; they carry no HTTP objects. ``app/core/handlers.py`` maps them to responses.
"""

from typing import Any


class AppException(Exception):  # noqa: N818  (the documented name)
    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    message: str = "Something went wrong. Please try again."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: Any = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details
        self.status_code = status_code or self.status_code
        self.headers = headers or {}
        super().__init__(self.message)


class AuthenticationError(AppException):
    status_code = 401
    code = "AUTH_REQUIRED"
    message = "Sign in to continue."


class AuthorizationError(AppException):
    status_code = 403
    code = "PERMISSION_DENIED"
    message = "You do not have permission to do that."


class NotFoundError(AppException):
    status_code = 404
    code = "RESOURCE_NOT_FOUND"
    message = "Not found."


class ConflictError(AppException):
    status_code = 409
    code = "CONFLICT"
    message = "This conflicts with the current state."


class ValidationError(AppException):
    status_code = 422
    code = "VALIDATION_ERROR"
    message = "Some fields are invalid."

    @classmethod
    def for_field(cls, field: str, message: str, code: str = "invalid") -> "ValidationError":
        return cls(details=[{"field": field, "code": code, "message": message}])


class RateLimitError(AppException):
    status_code = 429
    code = "RATE_LIMITED"
    message = "Too many attempts. Please wait and try again."

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            details={"retry_after": retry_after}, headers={"Retry-After": str(retry_after)}
        )


class FileUploadError(AppException):
    status_code = 400
    code = "FILE_INVALID"
    message = "The file could not be read."


class ExternalServiceError(AppException):
    status_code = 502
    code = "EXTERNAL_SERVICE_ERROR"
    message = "A service we depend on failed. Please try again."


class ServiceUnavailableError(ExternalServiceError):
    status_code = 503
    code = "SERVICE_UNAVAILABLE"
    message = "The service is temporarily unavailable. Please try again shortly."

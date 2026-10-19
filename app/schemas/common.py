"""Response envelope shared by every endpoint."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class PageMeta(BaseModel):
    next_cursor: str | None = None
    has_more: bool = False


class ApiResponse[T](BaseModel):
    success: bool = True
    data: T | None = None
    message: str | None = None
    meta: PageMeta | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorBody


class RequestModel(BaseModel):
    """Base for request bodies: unknown fields are rejected, strings are trimmed."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def ok[T](data: T | None = None, message: str | None = None) -> ApiResponse[T]:
    return ApiResponse[T](data=data, message=message)


def ok_page[T](items: list[T], next_cursor: str | None) -> ApiResponse[list[T]]:
    """A cursor-paginated list response."""
    return ApiResponse[list[T]](
        data=items, meta=PageMeta(next_cursor=next_cursor, has_more=next_cursor is not None)
    )


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI declarations for error statuses, all using the error envelope."""
    return {status: {"model": ErrorResponse} for status in (401, 422, 429, *statuses)}

"""Per-request context passed from routers to services, free of HTTP objects."""

from contextvars import ContextVar
from dataclasses import dataclass

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


@dataclass(frozen=True, slots=True)
class RequestContext:
    ip_address: str | None = None
    user_agent: str | None = None

    @property
    def request_id(self) -> str | None:
        return request_id_var.get()

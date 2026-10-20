"""Gzip for the JSON answers only.

The big answers (lists, reports, the schema) are JSON and shrink to a fraction on the wire. File
downloads (PDFs, Word and Excel files, receipts, exports) are served as they are stored, with their
own ``Content-Length``: they are already compressed or are streamed to a download.
"""

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipResponder
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _JsonOnlyResponder(GZipResponder):
    passthrough = False

    async def send_with_compression(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            content_type = Headers(raw=message["headers"]).get("content-type", "")
            self.passthrough = not content_type.lower().startswith("application/json")
        if self.passthrough:
            await self.send(message)
        else:
            await super().send_with_compression(message)


class JsonGZipMiddleware:
    def __init__(self, app: ASGIApp, minimum_size: int = 1024, compresslevel: int = 6) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or "gzip" not in Headers(scope=scope).get("accept-encoding", ""):
            await self.app(scope, receive, send)
            return
        responder = _JsonOnlyResponder(
            self.app, self.minimum_size, compresslevel=self.compresslevel
        )
        await responder(scope, receive, send)

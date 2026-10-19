"""A tiny stand-in for Mailpit: an SMTP server that keeps what it receives in memory and
serves the subset of Mailpit's HTTP API that the end-to-end tests use.

    python scripts/dev_mail_sink.py            # SMTP on :1025, HTTP on :8025

Use it when Docker or Mailpit are not available. It listens on 127.0.0.1 only and keeps
at most 500 messages. Endpoints (all JSON):

    GET    /api/v1/messages            newest first: {"messages": [{ID, Subject, ...}]}
    GET    /api/v1/message/{id}        {ID, Subject, To, From, Text, HTML, Created}
    DELETE /api/v1/messages            delete everything
"""

import argparse
import json
import threading
import uuid
from collections import deque
from datetime import UTC, datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from aiosmtpd.controller import Controller

MAX_MESSAGES = 500
_lock = threading.Lock()
_messages: deque[dict[str, Any]] = deque(maxlen=MAX_MESSAGES)


def _decoded(value: str | None) -> str:
    return str(make_header(decode_header(value or "")))


def _body(message: Message, kind: str) -> str:
    for part in message.walk():
        if part.get_content_type() == kind:
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                return payload.decode(part.get_content_charset() or "utf-8", "replace")
    return ""


class _Handler:
    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:  # noqa: N802
        message = message_from_bytes(envelope.content)
        record = {
            "ID": uuid.uuid4().hex,
            "Subject": _decoded(message["Subject"]),
            "From": _decoded(message["From"]),
            "To": [{"Address": address} for address in envelope.rcpt_tos],
            "Text": _body(message, "text/plain"),
            "HTML": _body(message, "text/html"),
            "Created": datetime.now(UTC).isoformat(),
        }
        with _lock:
            _messages.appendleft(record)
        return "250 OK"


class _Api(BaseHTTPRequestHandler):
    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        with _lock:
            if path == "/api/v1/messages":
                summaries = [
                    {k: m[k] for k in ("ID", "Subject", "From", "To", "Created")} for m in _messages
                ]
                self._send(200, {"total": len(summaries), "messages": summaries})
            elif path.startswith("/api/v1/message/"):
                wanted = path.rsplit("/", 1)[1]
                found = next((m for m in _messages if m["ID"] == wanted), None)
                self._send(200, found) if found else self._send(404, {"error": "not found"})
            else:
                self._send(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if urlsplit(self.path).path == "/api/v1/messages":
            with _lock:
                _messages.clear()
            self._send(200, {"deleted": True})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, format: str, *args: Any) -> None:
        return  # quiet


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--smtp-port", type=int, default=1025)
    parser.add_argument("--http-port", type=int, default=8025)
    args = parser.parse_args()
    smtp = Controller(
        _Handler(), hostname="127.0.0.1", port=args.smtp_port, server_hostname="localhost"
    )
    smtp.start()
    http = ThreadingHTTPServer(("127.0.0.1", args.http_port), _Api)
    print(
        f"mail sink: SMTP 127.0.0.1:{args.smtp_port}, API http://127.0.0.1:{args.http_port}",
        flush=True,
    )
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        smtp.stop()


if __name__ == "__main__":
    main()

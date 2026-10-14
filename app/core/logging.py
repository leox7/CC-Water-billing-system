"""Request ID, JSON log lines and the facts about the current request
(docs/AUDIT_LOG.md section 6).

Every request gets an ID. It is put in every log line and audit row of that request,
sent back in the X-Request-ID header, and shown as the "Reference" on the 500 page, so
the developer can find the exact request.
"""

import json
import logging
import re
import secrets
import sys
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
client_ip_var: ContextVar[str | None] = ContextVar("client_ip", default=None)

# Kenya has no daylight saving, so a fixed offset is right (see DEPLOYMENT.md section 8)
EAT = timezone(timedelta(hours=3))

# a Kenyan mobile number in any common spelling: 0722123456, 254722123456, +254722123456
PHONE_NUMBER_PATTERN = re.compile(r"(?<!\d)(?:\+?254|0)[17]\d{8}(?!\d)")

access_logger = logging.getLogger("app.access")


def mask_phone_numbers(text: str) -> str:
    """Full phone numbers never go into a log or an audit row (SECURITY.md LOG-2)."""
    return PHONE_NUMBER_PATTERN.sub("[phone removed]", text)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, so the journal can be searched with grep or jq."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.now(EAT).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "request_id": request_id_var.get(),
        }
        request_fields = getattr(record, "fields", None)
        if request_fields:
            entry.update(request_fields)
        else:
            entry["logger"] = record.name
            entry["message"] = mask_phone_numbers(record.getMessage())
        if record.exc_info:
            # an error adds the exception type, message and full stack trace (LOG-2 still applies)
            error = record.exc_info[1]
            entry["exc_type"] = type(error).__name__
            entry["exc_message"] = mask_phone_numbers(str(error))
            entry["stack_trace"] = mask_phone_numbers(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False, default=str)


class _StdoutHandler(logging.StreamHandler):
    """Writes to whatever sys.stdout is at that moment. systemd keeps it in the journal."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stdout
        super().emit(record)


def configure_logging() -> None:
    """Send every app.* logger to the journal as JSON lines. Safe to call more than once."""
    app_logger = logging.getLogger("app")
    if any(isinstance(handler, _StdoutHandler) for handler in app_logger.handlers):
        return
    handler = _StdoutHandler()
    handler.setFormatter(JsonFormatter())
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)


class RequestContextMiddleware:
    """Gives each request an ID, adds it to the response, and logs one line when it ends."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Made here, never taken from the request: a client could send anything
        request_id = secrets.token_hex(4)
        client = scope.get("client")
        id_token = request_id_var.set(request_id)
        ip_token = client_ip_var.set(client[0] if client else None)
        started = time.perf_counter()
        status = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            self._log_request(scope, status, started)
            request_id_var.reset(id_token)
            client_ip_var.reset(ip_token)

    def _log_request(self, scope: Scope, status: int, started: float) -> None:
        path = scope["path"]
        # Pages and actions are logged. The stylesheet, script and font fetches are not:
        # a single page view makes about ten of them and they say nothing useful.
        if path.startswith("/static/"):
            return
        session = scope.get("session") or {}
        level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
        access_logger.log(
            level,
            "request",
            extra={
                "fields": {
                    "user_id": session.get("user_id"),
                    "method": scope["method"],
                    "path": path,
                    "status": status,
                    "ms": round((time.perf_counter() - started) * 1000),
                }
            },
        )

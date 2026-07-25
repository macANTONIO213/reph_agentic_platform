"""
Observability primitives — correlation IDs + structured logging (plan E2).

Every request is stamped with a correlation id (from an inbound
``X-Request-ID`` / ``X-Correlation-ID`` header when present, else generated).
The id is:

  - stored in a ``contextvar`` so any log record on the request thread can carry
    it (see ``CorrelationIdFilter``);
  - echoed back on the response as ``X-Request-ID`` so a caller can quote it when
    reporting an issue;
  - available to view code via ``current_correlation_id()``.

Logging is configured in ``settings.LOGGING``; set ``LOG_FORMAT=json`` for
one-line JSON logs (correlation id, level, logger, message) suitable for a log
aggregator.
"""
from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")

_INBOUND_HEADERS = ("HTTP_X_REQUEST_ID", "HTTP_X_CORRELATION_ID")
_MAX_ID_LEN = 128


def current_correlation_id() -> str:
    return _correlation_id.get()


def _sanitize(value: str) -> str:
    # Keep only safe id characters; never let a header inject into log lines.
    cleaned = "".join(c for c in value if c.isalnum() or c in "-_")[:_MAX_ID_LEN]
    return cleaned or uuid.uuid4().hex


class CorrelationIdMiddleware:
    """Assign/propagate a correlation id for the lifetime of each request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = ""
        for header in _INBOUND_HEADERS:
            if request.META.get(header):
                incoming = request.META[header]
                break
        cid = _sanitize(incoming) if incoming else uuid.uuid4().hex
        token = _correlation_id.set(cid)
        request.correlation_id = cid
        try:
            response = self.get_response(request)
        finally:
            _correlation_id.reset(token)
        response["X-Request-ID"] = cid
        return response


class CorrelationIdFilter(logging.Filter):
    """Attach the current correlation id to every log record as ``correlation_id``."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = current_correlation_id()
        return True


class JsonLogFormatter(logging.Formatter):
    """Minimal structured JSON formatter — one object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": getattr(record, "correlation_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)

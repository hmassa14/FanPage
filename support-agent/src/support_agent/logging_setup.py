"""Structured JSON logging with a per-ticket trace id carried in a contextvar.

Every log line gets `trace_id` and `ticket_id` automatically, which is what lets you
grep one email's full journey through the system in production log tooling.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from typing import Any

trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
ticket_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("ticket_id", default=None)


def _otel_trace_id() -> str | None:
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        return format(ctx.trace_id, "032x") if ctx.is_valid else None
    except Exception:  # noqa: BLE001 - logging must never fail because tracing did
        return None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": trace_id_var.get(),
            "ticket_id": ticket_id_var.get(),
            "otel_trace_id": _otel_trace_id(),
        }
        extra = getattr(record, "data", None)
        if extra:
            payload["data"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = getattr(record, "data", None)
        tid = ticket_id_var.get()
        prefix = f"[{tid[:8]}] " if tid else ""
        return f"{prefix}{base}" + (f" {json.dumps(extra, default=str)}" if extra else "")


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter() if json_logs else PlainFormatter("%(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("httpx2").setLevel("WARNING")


def log(logger: logging.Logger, level: int, msg: str, **data: Any) -> None:
    logger.log(level, msg, extra={"data": data} if data else None)

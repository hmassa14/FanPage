"""OpenTelemetry tracing. One trace per ticket, one span per stage, one span per tool call.

Exporter is chosen by settings: `none` (no-op tracer, zero overhead), `console` (spans to
stderr, useful locally), or `otlp` (HTTP to a collector such as Jaeger at :4318).
The OTel trace id is also written into every JSON log line, so a log line and a trace
waterfall for the same ticket can be joined in any backend.
"""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter

from .config import Settings

logger = logging.getLogger(__name__)
_configured = False


def configure_tracing(s: Settings, exporter: SpanExporter | None = None) -> TracerProvider | None:
    """Idempotent. Pass an explicit exporter (tests) or rely on settings."""
    global _configured
    if _configured and exporter is None:
        return None
    if exporter is None:
        if s.otel_exporter == "console":
            exporter = ConsoleSpanExporter()
        elif s.otel_exporter == "otlp":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(endpoint=s.otel_endpoint.rstrip("/") + "/v1/traces")
        else:
            _configured = True
            return None  # API no-op tracer; nothing is recorded
    provider = TracerProvider(resource=Resource.create({"service.name": s.otel_service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _configured = True
    logger.info("tracing configured", extra={"data": {"exporter": s.otel_exporter}})
    return provider


def tracer() -> trace.Tracer:
    return trace.get_tracer("support_agent")


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None

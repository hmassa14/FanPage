"""OpenTelemetry tracing for the agent.

Three span kinds, following the OpenTelemetry GenAI semantic conventions
(attribute names come from `opentelemetry-semantic-conventions`, incubating):

    invoke_agent triage                 one per triage() call        (INTERNAL)
    ├── chat claude-opus-5              one per messages.create()    (CLIENT)
    ├── execute_tool lookup_customer    one per tool call            (INTERNAL)
    ├── execute_tool search_incidents
    └── chat claude-opus-5

The loop in agent.py only touches the OpenTelemetry *API* through the
helpers below. With no provider configured the API is a no-op, so tracing
costs nothing until `configure()` runs at an entry point (eval.py, agent.py
__main__) or a test installs an in-memory exporter.

Content (prompts, tool arguments, tool results, the decision JSON) is NOT
recorded by default: tickets carry customer data. Set
TRIAGE_OTEL_CAPTURE_CONTENT=1 to opt in, the same posture Claude Code takes
with its OTEL_LOG_* flags.
"""

from __future__ import annotations

import atexit
import json
import os
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as ga
from opentelemetry.trace import SpanKind, Status, StatusCode

AGENT_NAME = "triage"
PROVIDER = ga.GenAiProviderNameValues.ANTHROPIC.value  # "anthropic"

tracer = trace.get_tracer("triage-agent")


def capture_content() -> bool:
    return os.environ.get("TRIAGE_OTEL_CAPTURE_CONTENT", "") == "1"


# --------------------------------------------------------------------------- #
# Setup (call once, at an entry point)
# --------------------------------------------------------------------------- #


def configure(exporter: str | None = None, service_name: str = "triage-agent") -> Any:
    """Install a TracerProvider. exporter: "none" (default) | "console" | "otlp".

    "otlp" sends http/protobuf to OTEL_EXPORTER_OTLP_ENDPOINT (default
    http://localhost:4318), honouring OTEL_EXPORTER_OTLP_HEADERS. Returns the
    provider, or None when tracing stays off.
    """
    exporter = exporter or os.environ.get("TRIAGE_OTEL_EXPORTER", "none")
    if exporter == "none":
        return None

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif exporter == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    else:
        raise ValueError(f"unknown exporter {exporter!r}; use none, console, or otlp")

    trace.set_tracer_provider(provider)
    atexit.register(provider.shutdown)  # flush the batch on exit
    return provider


# --------------------------------------------------------------------------- #
# Spans the loop opens
# --------------------------------------------------------------------------- #


@contextmanager
def agent_span() -> Iterator[trace.Span]:
    with tracer.start_as_current_span(
        f"invoke_agent {AGENT_NAME}",
        kind=SpanKind.INTERNAL,
        attributes={
            ga.GEN_AI_OPERATION_NAME: ga.GenAiOperationNameValues.INVOKE_AGENT.value,
            ga.GEN_AI_AGENT_NAME: AGENT_NAME,
            ga.GEN_AI_PROVIDER_NAME: PROVIDER,
        },
    ) as span:
        yield span


@contextmanager
def chat_span(model: str, max_tokens: int) -> Iterator[trace.Span]:
    with tracer.start_as_current_span(
        f"chat {model}",
        kind=SpanKind.CLIENT,
        attributes={
            ga.GEN_AI_OPERATION_NAME: ga.GenAiOperationNameValues.CHAT.value,
            ga.GEN_AI_PROVIDER_NAME: PROVIDER,
            ga.GEN_AI_REQUEST_MODEL: model,
            ga.GEN_AI_REQUEST_MAX_TOKENS: max_tokens,
        },
    ) as span:
        yield span


def record_response(span: trace.Span, response: Any) -> None:
    """Attach what the API told us about the call. Cheap, no content."""
    span.set_attribute(ga.GEN_AI_RESPONSE_MODEL, response.model)
    span.set_attribute(ga.GEN_AI_RESPONSE_FINISH_REASONS, [response.stop_reason])
    if getattr(response, "id", None):
        span.set_attribute(ga.GEN_AI_RESPONSE_ID, response.id)
    usage = response.usage
    span.set_attribute(ga.GEN_AI_USAGE_INPUT_TOKENS, usage.input_tokens)
    span.set_attribute(ga.GEN_AI_USAGE_OUTPUT_TOKENS, usage.output_tokens)
    for attr, name in (
        (ga.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS, "cache_read_input_tokens"),
        (ga.GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS, "cache_creation_input_tokens"),
    ):
        value = getattr(usage, name, None)
        if value is not None:
            span.set_attribute(attr, value)


@contextmanager
def tool_span(name: str, call_id: str, arguments: dict) -> Iterator[trace.Span]:
    attributes = {
        ga.GEN_AI_OPERATION_NAME: ga.GenAiOperationNameValues.EXECUTE_TOOL.value,
        ga.GEN_AI_TOOL_NAME: name,
        ga.GEN_AI_TOOL_CALL_ID: call_id,
        ga.GEN_AI_TOOL_TYPE: "function",
    }
    if capture_content():
        attributes[ga.GEN_AI_TOOL_CALL_ARGUMENTS] = json.dumps(arguments)
    with tracer.start_as_current_span(f"execute_tool {name}", kind=SpanKind.INTERNAL, attributes=attributes) as span:
        yield span


def record_tool_outcome(span: trace.Span, is_error: bool, content: str) -> None:
    span.set_attribute("triage.tool.is_error", is_error)
    if capture_content():
        span.set_attribute(ga.GEN_AI_TOOL_CALL_RESULT, content)
    if is_error:
        span.set_status(Status(StatusCode.ERROR, content[:200]))


def record_decision(span: trace.Span, decision: Any, turns: int, raw_text: str) -> None:
    span.set_attribute("triage.turns", turns)
    span.set_attribute("triage.decision.category", decision.category)
    span.set_attribute("triage.decision.priority", decision.priority)
    span.set_attribute("triage.decision.needs_human", decision.needs_human)
    if capture_content():
        span.set_attribute(ga.GEN_AI_OUTPUT_MESSAGES, raw_text)


def record_failure(span: trace.Span, kind: str, detail: str) -> None:
    span.set_attribute("error.type", kind)
    span.set_status(Status(StatusCode.ERROR, f"{kind}: {detail}"[:200]))

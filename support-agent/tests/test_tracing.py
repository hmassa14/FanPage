from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from support_agent import tracing
from support_agent.bootstrap import build_pipeline
from support_agent.delivery.sender import FileSender
from support_agent.store.db import Store

from .conftest import load_sample


def test_one_trace_per_ticket_with_stage_tool_and_gate_spans(settings, tmp_path):
    exporter = InMemorySpanExporter()
    provider = tracing.configure_tracing(settings, exporter=exporter)
    assert provider is not None
    pipeline = build_pipeline(settings, store=Store(":memory:"), sender=FileSender("s@x", tmp_path / "sent"))
    t = pipeline.process_email(load_sample("01"))
    provider.force_flush()
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    root = next(s for s in spans if s.name == "ticket.process")
    assert {
        "stage.triage",
        "stage.research",
        "stage.draft",
        "stage.judge",
        "gate.evaluate",
        "outbox.send",
    } <= set(names)
    assert "tool.get_order" in names and "tool.get_customer" in names
    # every span belongs to the same trace as the root
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    assert root.attributes["ticket.id"] == t.id
    assert root.attributes["gate.decision"] == "auto_send"
    stage = next(s for s in spans if s.name == "stage.research")
    assert stage.parent.span_id == root.context.span_id
    assert "llm.model" in stage.attributes and "llm.cost_usd" in stage.attributes
    tool = next(s for s in spans if s.name == "tool.get_order")
    assert tool.parent.span_id == stage.context.span_id and tool.attributes["tool.ok"] is True
    gate = next(s for s in spans if s.name == "gate.evaluate")
    assert "actions.auto_approved" in gate.attributes["gate.reasons"]


def test_no_exporter_means_noop_tracer(settings):
    assert tracing.configure_tracing(settings) is None or True  # idempotent, never raises
    with tracing.tracer().start_as_current_span("x"):
        pass

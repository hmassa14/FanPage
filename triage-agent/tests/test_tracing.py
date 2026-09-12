"""Span shape, checked with an in-memory exporter. No collector, no network."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from agent import MODEL, AgentError, triage
from fake_client import ScriptedClient, final_json, response, text_block, tool_use_block
from schemas import Ticket

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)  # global, once; tracers acquired earlier are proxies and follow it

TICKET = Ticket(customer_id="cust_001", subject="CSV export never finishes", body="exports time out")
GOOD = {"category": "bug", "priority": "P0", "needs_human": True, "summary": "s", "evidence": ["e"]}


@pytest.fixture(autouse=True)
def _clear():
    EXPORTER.clear()
    yield


def _spans():
    return {s.name: s for s in EXPORTER.get_finished_spans()}


def test_span_tree_for_a_two_tool_run(monkeypatch):
    monkeypatch.delenv("TRIAGE_OTEL_CAPTURE_CONTENT", raising=False)
    first = response(
        [
            tool_use_block("toolu_A", "lookup_customer", {"customer_id": "cust_001"}),
            tool_use_block("toolu_B", "search_incidents", {"query": "csv export"}),
        ],
        "tool_use",
    )
    triage(TICKET, client=ScriptedClient([first, final_json(GOOD)]))

    spans = EXPORTER.get_finished_spans()
    names = sorted(s.name for s in spans)
    assert names == sorted([
        "invoke_agent triage",
        f"chat {MODEL}", f"chat {MODEL}",
        "execute_tool lookup_customer", "execute_tool search_incidents",
    ])

    root = _spans()["invoke_agent triage"]
    assert root.parent is None
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root.attributes["triage.decision.category"] == "bug"
    assert root.attributes["triage.turns"] == 2
    assert root.status.status_code == StatusCode.UNSET

    # every child hangs off the root, in the same trace
    for s in spans:
        if s is not root:
            assert s.parent.span_id == root.context.span_id
            assert s.context.trace_id == root.context.trace_id

    chats = [s for s in spans if s.name.startswith("chat ")]
    assert [s.attributes["gen_ai.response.finish_reasons"] for s in chats] == [("tool_use",), ("end_turn",)]
    assert all(s.attributes["gen_ai.provider.name"] == "anthropic" for s in chats)
    assert all(s.attributes["gen_ai.usage.input_tokens"] == 100 for s in chats)
    assert all(s.attributes["gen_ai.response.model"] == "fake-model" for s in chats)

    tool = _spans()["execute_tool lookup_customer"]
    assert tool.attributes["gen_ai.tool.call.id"] == "toolu_A"
    assert tool.attributes["triage.tool.is_error"] is False
    # content is off by default: no arguments, no results on the span
    assert "gen_ai.tool.call.arguments" not in tool.attributes
    assert "gen_ai.tool.call.result" not in tool.attributes


def test_content_capture_is_opt_in(monkeypatch):
    monkeypatch.setenv("TRIAGE_OTEL_CAPTURE_CONTENT", "1")
    first = response([tool_use_block("toolu_X", "lookup_customer", {"customer_id": "cust_404"})], "tool_use")
    triage(TICKET, client=ScriptedClient([first, final_json(GOOD)]))
    tool = _spans()["execute_tool lookup_customer"]
    assert '"cust_404"' in tool.attributes["gen_ai.tool.call.arguments"]
    assert "cust_404" in tool.attributes["gen_ai.tool.call.result"]
    assert tool.status.status_code == StatusCode.ERROR  # tool returned is_error
    assert "gen_ai.output.messages" in _spans()["invoke_agent triage"].attributes


def test_failure_marks_the_root_span():
    with pytest.raises(AgentError):
        triage(TICKET, client=ScriptedClient([response([text_block("{")], "max_tokens")]))
    root = _spans()["invoke_agent triage"]
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["error.type"] == "truncated"

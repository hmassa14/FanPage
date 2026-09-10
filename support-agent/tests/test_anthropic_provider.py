"""Exercises the real Claude provider code path against a stubbed SDK client.

No network. What this pins down: the request shape (model, adaptive thinking, effort,
cached system prompt, fallbacks beta), refusal handling, the one-retry-on-validation
behavior, and the research tool loop (dispatch, single-turn tool results, final
`tool_choice: none` brief with tools_used filled in).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pydantic
import pytest
from anthropic.types.beta import BetaMessage, BetaTextBlock, BetaToolUseBlock, BetaUsage

from support_agent.config import PROJECT_ROOT, Settings
from support_agent.knowledge.crm import CRM
from support_agent.knowledge.kb import KnowledgeBase
from support_agent.knowledge.tools import ResearchTools
from support_agent.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from support_agent.llm.base import ModelOutputError, RefusalError
from support_agent.models import (
    ExtractedEntities,
    RedactedEmail,
    ResearchBrief,
    Sentiment,
    TriageCategory,
    TriageResult,
    Urgency,
)


def _beta_message(content, stop_reason="end_turn", model="claude-opus-5") -> BetaMessage:
    return BetaMessage(
        id="msg_1",
        type="message",
        role="assistant",
        model=model,
        content=content,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=BetaUsage(
            input_tokens=1000, output_tokens=200, cache_read_input_tokens=800, cache_creation_input_tokens=0
        ),
    )


def _parsed(obj, stop_reason="end_turn"):
    """Shape of ParsedBetaMessage as far as the provider reads it."""
    block = SimpleNamespace(type="text", text=obj.model_dump_json(), parsed_output=obj)
    return SimpleNamespace(
        content=[block],
        stop_reason=stop_reason,
        model="claude-opus-5",
        usage=BetaUsage(
            input_tokens=500, output_tokens=100, cache_read_input_tokens=400, cache_creation_input_tokens=0
        ),
    )


class StubMessages:
    def __init__(self, parse_results, create_results=()):
        self.parse_results, self.create_results = list(parse_results), list(create_results)
        self.parse_calls, self.create_calls = [], []

    def parse(self, **kw):
        self.parse_calls.append(kw)
        r = self.parse_results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def create(self, **kw):
        self.create_calls.append(kw)
        return self.create_results.pop(0)


def _client(messages: StubMessages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


EMAIL = RedactedEmail(
    ticket_id="t1",
    from_address="maya.chen@example.com",
    from_name="Maya",
    subject="Return NW-10042",
    body_text="Please refund NW-10042",
    received_at=datetime.now(UTC),
)
TRIAGE = TriageResult(
    category=TriageCategory.return_or_refund,
    urgency=Urgency.normal,
    sentiment=Sentiment.neutral,
    language="en",
    summary="s",
    customer_ask="refund",
    entities=ExtractedEntities(order_ids=["NW-10042"]),
    confidence=0.95,
)
BRIEF = ResearchBrief(
    customer_context="c",
    order_context="o",
    relevant_policies=[],
    findings=[],
    recommended_resolution="refund",
)


def test_triage_request_shape_and_cost():
    stub = StubMessages(parse_results=[_parsed(TRIAGE)])
    p = AnthropicProvider(Settings(llm_provider="anthropic"), client=_client(stub))
    out = p.triage(EMAIL)
    kw = stub.parse_calls[0]
    assert kw["model"] == "claude-opus-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "low"}
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["output_format"] is TriageResult
    assert "temperature" not in kw and "budget_tokens" not in json.dumps(kw, default=str)
    assert out.result.category == TriageCategory.return_or_refund
    # 500 in * $5 + 400 cache-read * $0.5 + 100 out * $25, per MTok
    assert out.usage.cost_usd == pytest.approx((500 * 5 + 400 * 0.5 + 100 * 25) / 1e6)


def test_fallbacks_can_be_disabled():
    stub = StubMessages(parse_results=[_parsed(TRIAGE)])
    p = AnthropicProvider(Settings(llm_provider="anthropic", anthropic_fallbacks=False), client=_client(stub))
    p.triage(EMAIL)
    assert "betas" not in stub.parse_calls[0] and "fallbacks" not in stub.parse_calls[0]


def test_refusal_raises():
    stub = StubMessages(parse_results=[_parsed(TRIAGE, stop_reason="refusal")])
    p = AnthropicProvider(Settings(llm_provider="anthropic"), client=_client(stub))
    with pytest.raises(RefusalError):
        p.triage(EMAIL)


def test_validation_error_retries_once_then_gives_up():
    err = pydantic.ValidationError.from_exception_data("TriageResult", [])
    stub = StubMessages(parse_results=[err, _parsed(TRIAGE)])
    p = AnthropicProvider(Settings(llm_provider="anthropic"), client=_client(stub))
    assert p.triage(EMAIL).result.confidence == 0.95 and len(stub.parse_calls) == 2
    stub2 = StubMessages(parse_results=[err, err])
    p2 = AnthropicProvider(Settings(llm_provider="anthropic"), client=_client(stub2))
    with pytest.raises(ModelOutputError):
        p2.triage(EMAIL)


def test_research_tool_loop_dispatches_and_finalizes():
    tool_turn = _beta_message(
        [
            BetaToolUseBlock(id="tu_1", type="tool_use", name="get_order", input={"order_id": "NW-10042"}),
            BetaToolUseBlock(id="tu_2", type="tool_use", name="get_customer", input={}),
        ],
        stop_reason="tool_use",
    )
    done_turn = _beta_message([BetaTextBlock(type="text", text="enough")])
    stub = StubMessages(parse_results=[_parsed(BRIEF)], create_results=[tool_turn, done_turn])
    p = AnthropicProvider(Settings(llm_provider="anthropic"), client=_client(stub))
    tools = ResearchTools(
        kb=KnowledgeBase(PROJECT_ROOT / "data" / "kb"),
        crm=CRM(PROJECT_ROOT / "data" / "crm"),
        customer_email=EMAIL.from_address,
    )
    out = p.research(EMAIL, TRIAGE, tools)

    assert out.result.tools_used == ["get_order", "get_customer"]
    # both tool results went back in ONE user turn, in order, with is_error False
    second_call_msgs = stub.create_calls[1]["messages"]
    results = second_call_msgs[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2"]
    assert all(r["type"] == "tool_result" and r["is_error"] is False for r in results)
    assert json.loads(results[0]["content"])["found"] is True
    # final call: tools still attached, tool_choice none, typed output
    final = stub.parse_calls[0]
    assert final["tool_choice"] == {"type": "none"} and final["output_format"] is ResearchBrief
    assert final["tools"][0]["strict"] is True
    # usage accumulated across 2 create calls + 1 parse call
    assert out.usage.input_tokens == 1000 + 1000 + 500

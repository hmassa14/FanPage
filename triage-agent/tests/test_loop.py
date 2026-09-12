"""The three ways the manual loop breaks, each pinned by a test."""

import json

import pytest

from agent import AgentError, triage
from fake_client import ScriptedClient, final_json, response, text_block, tool_use_block
from schemas import Ticket

TICKET = Ticket(customer_id="cust_001", subject="CSV export never finishes", body="exports time out")
GOOD = {
    "category": "bug",
    "priority": "P0",
    "needs_human": True,
    "summary": "Enterprise export outage",
    "evidence": ["plan=enterprise", "INC-2291"],
}


def test_parallel_tool_calls_produce_one_user_message_with_matching_ids():
    first = response(
        [
            text_block("Checking the account and incidents."),
            tool_use_block("toolu_A", "lookup_customer", {"customer_id": "cust_001"}),
            tool_use_block("toolu_B", "search_incidents", {"query": "csv export timeout"}),
        ],
        "tool_use",
    )
    client = ScriptedClient([first, final_json(GOOD)])

    result = triage(TICKET, client=client)

    assert result.decision.category == "bug"
    # transcript: user, assistant (verbatim), user (tool results), assistant (final)
    assert [m["role"] for m in result.messages] == ["user", "assistant", "user", "assistant"]
    # 1. the assistant turn is the response content object itself, text block included
    assert result.messages[1]["content"] is first.content
    # 2. ONE user message carrying one tool_result per tool_use ...
    results = result.messages[2]["content"]
    assert [r["type"] for r in results] == ["tool_result", "tool_result"]
    # 3. ... with ids matched, in order
    assert [r["tool_use_id"] for r in results] == ["toolu_A", "toolu_B"]
    assert all(r["is_error"] is False for r in results)
    assert json.loads(results[0]["content"])["plan"] == "enterprise"
    # the second request carried the full transcript
    assert len(client.messages.calls[1]["messages"]) == 3


def test_tool_error_is_returned_as_is_error_result_not_raised():
    first = response([tool_use_block("toolu_X", "lookup_customer", {"customer_id": "cust_404"})], "tool_use")
    client = ScriptedClient([first, final_json(GOOD)])
    result = triage(TICKET, client=client)
    tr = result.messages[2]["content"][0]
    assert tr["is_error"] is True and tr["tool_use_id"] == "toolu_X"
    assert result.tool_calls[0].is_error


def test_request_shape_has_strict_tools_and_output_format():
    client = ScriptedClient([final_json(GOOD)])
    triage(TICKET, client=client)
    req = client.messages.calls[0]
    assert all(t["strict"] is True for t in req["tools"])
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert req["messages"][0] == {"role": "user", "content": TICKET.render()}


def test_invalid_json_output_is_a_classified_failure():
    bad = dict(GOOD, category="urgent")  # not in the enum
    client = ScriptedClient([final_json(bad)])
    with pytest.raises(AgentError) as exc:
        triage(TICKET, client=client)
    assert exc.value.kind == "invalid_output"


def test_refusal_and_truncation_are_classified():
    with pytest.raises(AgentError) as exc:
        triage(TICKET, client=ScriptedClient([response([], "refusal")]))
    assert exc.value.kind == "refusal"
    with pytest.raises(AgentError) as exc:
        triage(TICKET, client=ScriptedClient([response([text_block("{")], "max_tokens")]))
    assert exc.value.kind == "truncated"


def test_max_turns_guard():
    loop_forever = response([tool_use_block("t", "lookup_customer", {"customer_id": "cust_001"})], "tool_use")
    client = ScriptedClient([loop_forever] * 10)
    with pytest.raises(AgentError) as exc:
        triage(TICKET, client=client, max_turns=3)
    assert exc.value.kind == "max_turns"
    assert len(client.messages.calls) == 3

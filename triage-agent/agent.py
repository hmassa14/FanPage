"""The manual tool-use loop. This is the file to delete and rewrite cold.

The loop is four moves, and the API is strict about all four:

  1. Send the transcript. Claude answers with `stop_reason` and `content`.
  2. If `stop_reason == "tool_use"`: append the assistant turn VERBATIM
     (the whole `response.content` list, not just the tool_use blocks).
  3. Execute every `tool_use` block, then append ONE user message whose
     content is a `tool_result` per `tool_use`, each carrying the matching
     `tool_use_id`. Split them across messages or drop one and the next
     request is a 400.
  4. If `stop_reason == "end_turn"`: the text block is JSON shaped by
     `output_config.format`. Validate it with Pydantic anyway.

Everything else is bookkeeping so the harness can tell a model failure from
a plumbing failure.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import ValidationError

from schemas import DECISION_SCHEMA, Ticket, TriageDecision
from tools import TOOL_DEFINITIONS, execute
import tracing

MODEL = os.environ.get("TRIAGE_MODEL", "claude-opus-5")
MAX_TOKENS = 4096
MAX_TURNS = 6  # round-trips, not tool calls; parallel calls share a turn

SYSTEM_PROMPT = """You triage inbound support tickets for a B2B SaaS product.

Categories (pick exactly one):
- billing: charges, invoices, refunds, plan changes, failed payments.
- bug: something that should work does not, and it is not a known open incident being handled already. A ticket that matches an open incident is STILL a bug - mark it P0/P1 and cite the incident id.
- feature_request: asking for capability that does not exist.
- account_access: login, SSO, password, permissions, locked accounts. An SSO or login failure is account_access even when it matches an open incident; cite the incident and raise priority instead of recategorising.
- churn_risk: the customer signals they may leave (cancel, downgrade, "evaluating alternatives", "not renewing"). This wins over every other category when the signal is explicit, regardless of what the underlying complaint is.

Priority:
- P0: enterprise customer blocked or in an active incident, or churn risk from any paying customer with renewal inside 30 days.
- P1: paying customer materially impaired, or churn risk with renewal beyond 30 days.
- P2: paying customer inconvenienced, or any free-tier bug/billing/access issue.
- P3: feature requests and free-tier wishlist items.

Process:
1. Always call lookup_customer with the customer_id from the ticket before deciding. Plan and renewal window change the priority.
2. If the ticket describes something broken, slow, or failing, call search_incidents with a few keywords from the symptom.
3. Then respond with the triage decision. Put tool-derived facts (plan, MRR, renewal window, incident ids) in `evidence`."""


class AgentError(Exception):
    """A run that produced no scorable decision. `kind` is the failure class."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict
    is_error: bool
    content: str


@dataclass
class TriageResult:
    decision: TriageDecision
    tool_calls: list[ToolCall]
    messages: list[dict]  # full transcript, for the harness
    model: str
    turns: int
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    raw_text: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def _request_params() -> dict:
    """Everything that is the same on every call. Keeping this stable is what
    lets prompt caching hit: tools -> system -> messages is the cache prefix."""
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "tools": TOOL_DEFINITIONS,
        "output_config": {
            "effort": "low",  # classification: low effort is the documented sweet spot
            "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
        },
    }


def triage(ticket: Ticket, client: Any | None = None, max_turns: int = MAX_TURNS) -> TriageResult:
    """Run the loop once for one ticket. Raises AgentError on any non-scorable outcome.

    Wrapped in one `invoke_agent` span; every API call and tool call below
    opens a child span. See tracing.py for what gets recorded.
    """
    with tracing.agent_span() as span:
        try:
            return _triage(ticket, client, max_turns, span)
        except AgentError as exc:
            tracing.record_failure(span, exc.kind, exc.detail)
            raise


def _triage(ticket: Ticket, client: Any | None, max_turns: int, root: Any) -> TriageResult:
    client = client or anthropic.Anthropic()
    messages: list[dict] = [{"role": "user", "content": ticket.render()}]
    tool_calls: list[ToolCall] = []
    in_tok = out_tok = 0
    served_model = MODEL
    started = time.perf_counter()

    for turn in range(1, max_turns + 1):
        with tracing.chat_span(MODEL, MAX_TOKENS) as span:
            try:
                response = client.messages.create(**_request_params(), messages=messages)
            except anthropic.APIStatusError as exc:  # 4xx/5xx after the SDK's own retries
                tracing.record_failure(span, "api_error", str(exc.status_code))
                raise AgentError("api_error", f"{exc.status_code}: {exc.message}") from exc
            except anthropic.APIConnectionError as exc:
                tracing.record_failure(span, "connection_error", str(exc))
                raise AgentError("connection_error", str(exc)) from exc
            tracing.record_response(span, response)

        in_tok += response.usage.input_tokens
        out_tok += response.usage.output_tokens
        served_model = response.model

        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            raise AgentError("refusal", getattr(detail, "category", "") or "")
        if response.stop_reason == "max_tokens":
            raise AgentError("truncated", f"hit max_tokens={MAX_TOKENS} on turn {turn}")

        if response.stop_reason == "tool_use":
            # (2) the assistant turn goes back exactly as received
            messages.append({"role": "assistant", "content": response.content})

            # (3) one tool_result per tool_use, ids matched, all in ONE user message
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                with tracing.tool_span(block.name, block.id, dict(block.input)) as span:
                    outcome = execute(block.name, block.input)
                    tracing.record_tool_outcome(span, outcome.is_error, outcome.content)
                tool_calls.append(ToolCall(block.id, block.name, dict(block.input), outcome.is_error, outcome.content))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": outcome.content,
                        "is_error": outcome.is_error,
                    }
                )
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "pause_turn":
            # only server tools produce this; we have none, but the shape is:
            # append the partial assistant turn and re-send so Claude resumes.
            messages.append({"role": "assistant", "content": response.content})
            continue

        # (4) end_turn: the text block is schema-shaped JSON. Trust, then verify.
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            decision = TriageDecision.model_validate_json(text)
        except ValidationError as exc:
            raise AgentError("invalid_output", f"{exc.error_count()} validation errors; raw={text[:200]!r}") from exc

        messages.append({"role": "assistant", "content": response.content})
        tracing.record_decision(root, decision, turn, text)
        return TriageResult(
            decision=decision,
            tool_calls=tool_calls,
            messages=messages,
            model=served_model,
            turns=turn,
            input_tokens=in_tok,
            output_tokens=out_tok,
            latency_s=time.perf_counter() - started,
            raw_text=text,
        )

    raise AgentError("max_turns", f"no decision after {max_turns} turns")


if __name__ == "__main__":
    import json
    import sys

    tracing.configure()  # TRIAGE_OTEL_EXPORTER=console|otlp to see spans
    ticket = Ticket.model_validate_json(sys.stdin.read())
    result = triage(ticket)
    print(json.dumps(result.decision.model_dump(), indent=2))
    print(f"\n# {len(result.tool_calls)} tool call(s), {result.turns} turn(s), "
          f"{result.input_tokens} in / {result.output_tokens} out, served by {result.model}", file=sys.stderr)

"""Same agent, but the SDK drives the loop: `client.beta.messages.tool_runner`.

`@beta_tool` reads the function signature and docstring and generates the
tool definition; the runner calls the API, executes whichever tool Claude
asked for, appends the results, and repeats until `end_turn`. The whole of
agent.py's `for turn in ...` block becomes `runner.until_done()`.

Why you would still drop to the manual loop (agent.py):
  - it's beta (`client.beta.*`), and the request shapes it can build are the
    ones it knows about;
  - you don't get the transcript back unless you mirror it yourself (see
    `history` below), and the eval harness needs the transcript;
  - per-turn control (a human approval gate, retry policy, error routing,
    counting turns for a budget) is easier to reason about when the loop is
    twelve lines you own;
  - the runner does not auto-resume `pause_turn` in the Python SDK.

Usage: echo '{"customer_id": "cust_001", "subject": "...", "body": "..."}' | python agent_runner.py
"""

from __future__ import annotations

import json
import sys

import anthropic
from anthropic import beta_tool

from agent import MAX_TOKENS, MODEL, SYSTEM_PROMPT
from schemas import DECISION_SCHEMA, LookupCustomerInput, SearchIncidentsInput, Ticket, TriageDecision
from tools import execute


# The decorator builds the schema from the signature + docstring. The handlers
# are the same registry handlers; `execute` keeps error handling identical.


@beta_tool
def lookup_customer(customer_id: str) -> str:
    """Fetch the account record for the customer who filed the ticket. Call this first for every ticket.

    Args:
        customer_id: The customer_id exactly as it appears on the ticket.
    """
    return execute("lookup_customer", LookupCustomerInput(customer_id=customer_id).model_dump()).content


@beta_tool
def search_incidents(query: str) -> str:
    """Search currently open platform incidents by keyword. Call this whenever a ticket describes something broken, slow, or failing.

    Args:
        query: Two to five keywords describing the product area or symptom.
    """
    return execute("search_incidents", SearchIncidentsInput(query=query).model_dump()).content


def triage_with_runner(ticket: Ticket, client: anthropic.Anthropic | None = None) -> tuple[TriageDecision, list[dict]]:
    client = client or anthropic.Anthropic()
    history: list[dict] = [{"role": "user", "content": ticket.render()}]

    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        tools=[lookup_customer, search_incidents],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
        messages=history,
    )

    final = None
    for message in runner:  # one iteration per assistant turn
        final = message
        history.append({"role": "assistant", "content": message.content})
        tool_response = runner.generate_tool_call_response()  # cached: tools still run once
        if tool_response is not None:
            history.append(tool_response)

    if final is None or final.stop_reason != "end_turn":
        raise RuntimeError(f"runner ended with stop_reason={getattr(final, 'stop_reason', None)!r}")
    text = "".join(b.text for b in final.content if b.type == "text")
    return TriageDecision.model_validate_json(text), history


if __name__ == "__main__":
    decision, _ = triage_with_runner(Ticket.model_validate_json(sys.stdin.read()))
    print(json.dumps(decision.model_dump(), indent=2))

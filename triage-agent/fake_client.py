"""Offline stand-ins for `anthropic.Anthropic()`.

They expose the one method the loop uses, `client.messages.create(**kwargs)`,
and return objects with the attributes the loop reads: `content`,
`stop_reason`, `model`, `usage`. Nothing here talks to the network.

  ScriptedClient - returns a fixed list of responses in order. For testing the
                   loop's transcript handling.
  OracleClient   - knows the labelled cases and answers them perfectly, calling
                   the required tools first. Run the harness against it and
                   every metric must read 1.0; that proves the plumbing before
                   spending money. `plant` lets a test inject known failures.
  NullClient     - always answers the majority class with no tool calls. The
                   harness must score this badly; if it doesn't, the grader is
                   too lenient.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from schemas import Ticket

FAKE_MODEL = "fake-model"


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_use_block(id: str, name: str, input: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def response(content: list, stop_reason: str, model: str = FAKE_MODEL) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        model=model,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        stop_details=None,
    )


def final_json(decision: dict) -> SimpleNamespace:
    return response([text_block(json.dumps(decision))], "end_turn")


class _Messages:
    def __init__(self, fn: Callable[..., SimpleNamespace]):
        self._fn = fn
        self.calls: list[dict] = []

    def create(self, **kwargs) -> SimpleNamespace:
        # snapshot: the loop mutates its `messages` list after this call returns
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._fn(**kwargs)


class ScriptedClient:
    def __init__(self, responses: list[SimpleNamespace]):
        self._queue = list(responses)
        self.messages = _Messages(self._next)

    def _next(self, **kwargs) -> SimpleNamespace:
        if not self._queue:
            raise AssertionError("ScriptedClient ran out of responses")
        return self._queue.pop(0)


@dataclass
class Plant:
    """A deliberate failure for one case. Any field left None keeps oracle behaviour."""

    category: str | None = None
    priority: str | None = None
    skip_tools: bool = False


@dataclass
class OracleClient:
    cases: list[dict]
    plant: dict[str, Plant] = field(default_factory=dict)

    def __post_init__(self):
        self._by_ticket = {Ticket.model_validate(c["ticket"]).render(): c for c in self.cases}
        self.messages = _Messages(self._answer)

    def _answer(self, messages: list[dict], **_: Any) -> SimpleNamespace:
        case = self._by_ticket[messages[0]["content"]]
        plant = self.plant.get(case["id"], Plant())
        first_turn = len(messages) == 1

        if first_turn and not plant.skip_tools:
            ticket = case["ticket"]
            blocks = []
            for i, name in enumerate(case["required_tools"]):
                args = (
                    {"customer_id": ticket["customer_id"]}
                    if name == "lookup_customer"
                    else {"query": ticket["subject"]}
                )
                blocks.append(tool_use_block(f"toolu_{case['id']}_{i}", name, args))
            return response(blocks, "tool_use")

        expected = case["expected"]
        return final_json(
            {
                "category": plant.category or expected["category"],
                "priority": plant.priority or expected["priority"],
                "needs_human": expected["priority"] in ("P0", "P1"),
                "summary": f"oracle answer for {case['id']}",
                "evidence": ["oracle"],
            }
        )


class NullClient:
    def __init__(self, category: str = "billing", priority: str = "P2"):
        self._decision = {
            "category": category,
            "priority": priority,
            "needs_human": False,
            "summary": "constant answer",
            "evidence": [],
        }
        self.messages = _Messages(lambda **_: final_json(self._decision))

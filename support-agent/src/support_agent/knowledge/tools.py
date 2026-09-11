"""Tools exposed to the research stage.

Design rules (see docs/architecture.md):
  * every tool is read-only;
  * every tool is scoped to the requesting customer (no cross-customer lookups);
  * every call is recorded so the audit log shows exactly what the model saw.
Tool descriptions state *when* to call them, not just what they do; current models
trigger tools more reliably with explicit trigger conditions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..tracing import tracer
from .crm import CRM
from .kb import KnowledgeBase


@dataclass
class ToolCallRecord:
    name: str
    input: dict[str, Any]
    output: str
    ok: bool


@dataclass
class ResearchTools:
    kb: KnowledgeBase
    crm: CRM
    customer_email: str
    calls: list[ToolCallRecord] = field(default_factory=list)

    # ---- tool implementations ------------------------------------------- #
    def search_knowledge_base(self, query: str, top_k: int = 4) -> str:
        hits = self.kb.search(query, top_k=top_k)
        if not hits:
            return json.dumps({"results": [], "note": "No matching policy sections."})
        return json.dumps(
            {
                "results": [
                    {"doc_id": c.doc_id, "title": c.title, "section": c.section, "text": c.text}
                    for c, _ in hits
                ]
            }
        )

    def get_policy_document(self, doc_id: str) -> str:
        chunks = self.kb.get_doc(doc_id)
        if not chunks:
            return json.dumps({"error": f"Unknown doc_id '{doc_id}'", "known": self.kb.doc_ids()})
        return json.dumps(
            {
                "doc_id": doc_id,
                "title": chunks[0].title,
                "sections": [{"section": c.section, "text": c.text} for c in chunks],
            }
        )

    def get_customer(self) -> str:
        c = self.crm.get_customer(self.customer_email)
        if not c:
            return json.dumps({"found": False, "email": self.customer_email})
        return json.dumps({"found": True, **c})

    def list_customer_orders(self) -> str:
        orders = self.crm.list_orders_for(self.customer_email)
        return json.dumps(
            {"orders": [{k: o[k] for k in ("order_id", "placed_at", "status", "total_usd")} for o in orders]}
        )

    def get_order(self, order_id: str) -> str:
        if not self.crm.order_belongs_to(order_id, self.customer_email):
            # Do not reveal whether the order exists for someone else.
            return json.dumps(
                {
                    "found": False,
                    "order_id": order_id,
                    "note": "No order with this id on the customer's account.",
                }
            )
        return json.dumps({"found": True, **(self.crm.get_order(order_id) or {})})

    # ---- dispatch --------------------------------------------------------- #
    def dispatch(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        with tracer().start_as_current_span(f"tool.{name}") as span:
            span.set_attribute("tool.input", json.dumps(tool_input, sort_keys=True))
            out, ok = self._dispatch(name, tool_input)
            span.set_attribute("tool.ok", ok)
            span.set_attribute("tool.output_chars", len(out))
        return out, ok

    def _dispatch(self, name: str, tool_input: dict[str, Any]) -> tuple[str, bool]:
        try:
            if name == "search_knowledge_base":
                out = self.search_knowledge_base(str(tool_input["query"]))
            elif name == "get_policy_document":
                out = self.get_policy_document(str(tool_input["doc_id"]))
            elif name == "get_customer":
                out = self.get_customer()
            elif name == "list_customer_orders":
                out = self.list_customer_orders()
            elif name == "get_order":
                out = self.get_order(str(tool_input["order_id"]))
            else:
                raise KeyError(f"unknown tool {name}")
            ok = True
        except Exception as exc:  # noqa: BLE001 - report tool failures to the model
            out, ok = json.dumps({"error": f"{type(exc).__name__}: {exc}"}), False
        self.calls.append(ToolCallRecord(name=name, input=dict(tool_input), output=out, ok=ok))
        return out, ok

    def names_used(self) -> list[str]:
        seen: dict[str, None] = {}
        for c in self.calls:
            seen.setdefault(c.name, None)
        return list(seen)


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


# Anthropic tool definitions. `strict: True` guarantees schema-valid inputs.
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search company policies and help articles. Call this BEFORE stating any policy "
            "(return windows, shipping times, refund rules, warranty terms). Use a short "
            "natural-language query such as 'return window after delivery'."
        ),
        "strict": True,
        "input_schema": _obj({"query": {"type": "string"}}, ["query"]),
    },
    {
        "name": "get_policy_document",
        "description": (
            "Fetch a full policy document by id when a search hit is relevant but you need the "
            "surrounding sections (for example exceptions or exact thresholds)."
        ),
        "strict": True,
        "input_schema": _obj({"doc_id": {"type": "string"}}, ["doc_id"]),
    },
    {
        "name": "get_customer",
        "description": (
            "Look up the sender's CRM profile (tier, tenure, order count, notes). Call this once "
            "at the start of every investigation so the reply can be personalised correctly."
        ),
        "strict": True,
        "input_schema": _obj({}, []),
    },
    {
        "name": "list_customer_orders",
        "description": (
            "List the sender's recent orders. Call this when the email references an order but "
            "gives no order number, or the given order number is not found."
        ),
        "strict": True,
        "input_schema": _obj({}, []),
    },
    {
        "name": "get_order",
        "description": (
            "Fetch one order (items, status, shipping dates, tracking, total, days since "
            "delivery). Call this for every order number mentioned in the email."
        ),
        "strict": True,
        "input_schema": _obj({"order_id": {"type": "string"}}, ["order_id"]),
    },
]

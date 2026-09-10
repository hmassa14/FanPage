"""Mock CRM / order-management system.

In production this is your Shopify/Zendesk/internal API. The interface is intentionally
narrow and read-only: the research agent may look things up but can never mutate state.
Mutations (refunds, cancellations) are `ProposedAction`s that execute only after gates.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


class CRM:
    def __init__(self, crm_dir: Path, today: date | None = None) -> None:
        self.customers: dict[str, dict[str, Any]] = {
            c["email"].lower(): c
            for c in json.loads((crm_dir / "customers.json").read_text(encoding="utf-8"))
        }
        self.orders: dict[str, dict[str, Any]] = {
            o["order_id"].upper(): o
            for o in json.loads((crm_dir / "orders.json").read_text(encoding="utf-8"))
        }
        self.today = today or datetime.now(UTC).date()

    def get_customer(self, email: str) -> dict[str, Any] | None:
        return self.customers.get(email.lower())

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        order = self.orders.get(order_id.strip().upper())
        if not order:
            return None
        enriched = dict(order)
        if order.get("delivered_at"):
            delivered = date.fromisoformat(order["delivered_at"])
            enriched["days_since_delivery"] = (self.today - delivered).days
        return enriched

    def list_orders_for(self, email: str) -> list[dict[str, Any]]:
        return sorted(
            (o for o in self.orders.values() if o["customer_email"].lower() == email.lower()),
            key=lambda o: o["placed_at"],
            reverse=True,
        )

    def order_belongs_to(self, order_id: str, email: str) -> bool:
        o = self.orders.get(order_id.strip().upper())
        return bool(o and o["customer_email"].lower() == email.lower())
